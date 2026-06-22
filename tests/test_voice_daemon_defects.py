# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import contextlib
import gc
import logging
import sys
import time
import types
import weakref

import pytest


if "httpx" not in sys.modules:
    httpx = types.ModuleType("httpx")

    class _Timeout:
        def __init__(self, *args, **kwargs) -> None:
            self.args = args
            self.kwargs = kwargs

    httpx.Timeout = _Timeout
    sys.modules["httpx"] = httpx
if "sounddevice" not in sys.modules:
    sys.modules["sounddevice"] = types.ModuleType("sounddevice")
if "rapidfuzz" not in sys.modules:
    rapidfuzz = types.ModuleType("rapidfuzz")
    rapidfuzz.fuzz = types.SimpleNamespace()
    sys.modules["rapidfuzz"] = rapidfuzz


from jasper.voice.session import LiveConnection, LiveTurn  # noqa: E402
from jasper.voice_daemon import (  # noqa: E402
    State,
    WakeLoop,
    _idle_watchdog,
    _server_vad_response_trigger,
)


async def test_fire_and_forget_task_survives_gc_until_done():
    """WakeLoop keeps one-shot tasks strongly referenced until completion.

    The acquire/drain task sets `_acquiring=True` before it starts. If
    asyncio's weak task reference let it disappear mid-flight, the daemon
    would keep routing mic frames into the acquire buffer indefinitely.
    """
    wl = WakeLoop.for_tests()
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
    wl = WakeLoop.for_tests()
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
    wl = WakeLoop.for_tests()
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
            _idle_watchdog(
                turn,
                _DrainedTts(),
                timeout=999.0,
                response_stall_timeout=0.01,
            ),
            timeout=1.0,
        )

    assert "response stalled" in caplog.text


async def test_server_vad_trigger_uses_public_create_response_only():
    created = asyncio.Event()

    class _Turn:
        async def wait_for_server_eou(self) -> None:
            return None

        def turn_lost(self) -> bool:
            return False

    class _Connection:
        async def create_response_only(self) -> None:
            created.set()

    task = asyncio.create_task(
        _server_vad_response_trigger(_Turn(), _Connection()),
    )
    await asyncio.wait_for(created.wait(), timeout=1.0)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def test_live_protocols_declare_public_server_vad_shadow_members():
    assert hasattr(LiveTurn, "mark_server_vad")
    assert hasattr(LiveTurn, "server_speech_started")
    assert hasattr(LiveTurn, "wait_for_server_eou")
    assert hasattr(LiveConnection, "set_turn_detection")
    assert hasattr(LiveConnection, "create_response_only")


def test_gemini_does_not_inherit_optional_server_vad_shadow_members():
    pytest.importorskip("google.genai")

    from jasper.voice.gemini_session import GeminiLiveConnection, GeminiLiveTurn

    assert not hasattr(GeminiLiveTurn, "mark_server_vad")
    assert not hasattr(GeminiLiveTurn, "server_speech_started")
    assert not hasattr(GeminiLiveTurn, "wait_for_server_eou")
    assert not hasattr(GeminiLiveConnection, "set_turn_detection")
    assert not hasattr(GeminiLiveConnection, "create_response_only")

    conn = GeminiLiveConnection(api_key="fake", model="fake")
    assert conn.supports_server_vad() is False


async def test_turn_open_failure_cue_is_honest_about_cause():
    """Regression for the 2026-06-19 incident.

    An UNEXPECTED local error during turn-open (the trigger that day was
    a readonly usage.db write) used to fire the `cant_connect` cue — the
    speaker told the user "I can't connect right now, I'll keep trying"
    when connectivity was fine. The turn-open catch-all must pick the cue
    by the LIVE connection state: `cant_connect` only when the connection
    is genuinely paused, otherwise the honest, low-alarm `internal_error`
    cue. (Layer 2 separately keeps the usage write from reaching here at
    all; this pins the cue honesty regardless of what throws.)"""

    async def _drive(*, paused: bool) -> list[str]:
        wl = WakeLoop.for_tests()
        played: list[str] = []

        async def _rec(slug: str) -> None:
            played.append(slug)

        async def _win(**_kwargs) -> str:
            return "WIN"

        async def _noop(*_args, **_kwargs) -> None:
            return None

        async def _begin_boom() -> None:
            # Stands in for the real incident: an unexpected local failure
            # on the turn-open hot path (the connection itself is fine).
            raise RuntimeError("attempt to write a readonly database")

        class _Conn:
            def __init__(self, is_paused: bool) -> None:
                self._paused = is_paused

            def is_paused(self) -> bool:
                return self._paused

        wl._wake_late_cancelled = lambda *_a, **_k: False
        wl._peer_arbitrate = _win
        wl._prepare_assistant_loudness_context = _noop
        wl._play_listening_chirp = _noop
        wl._begin_turn = _begin_boom
        wl._play_cue = _rec
        wl._connection = _Conn(paused)

        try:
            await wl._arbitrate_acquire_drain(
                score=0.9,
                rms_dbfs=-30.0,
                spend_allowed=True,
                conn_paused=False,  # snapshot said fine; the failure is local
                can_serve=True,
            )
        finally:
            await wl._cancel_fire_and_forget_tasks()
        return played

    # Healthy connection + unexpected local error -> honest internal cue,
    # NOT a false "I can't connect".
    assert await _drive(paused=False) == ["internal_error"]

    # Connection genuinely dropped into paused/failed mid-acquire ->
    # cant_connect is the truthful cue.
    assert await _drive(paused=True) == ["cant_connect"]


def test_session_status_surfaces_usage_tracking_degraded():
    """session_status() exposes the UsageStore write-health so /state.voice (and
    the spend-cap UI) can show that spend recording is degraded — the S1 signal.
    Defaults False; reflects the store's write_degraded."""
    wl = WakeLoop.for_tests()
    assert wl.session_status()["usage_tracking_degraded"] is False

    class _DegradedStore:
        write_degraded = True

        def open_session(self, *_a, **_k):
            return 1

        def close_session(self, *_a, **_k):
            return 0.0

    wl._usage_store = _DegradedStore()
    assert wl.session_status()["usage_tracking_degraded"] is True
