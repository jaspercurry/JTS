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
    wl = WakeLoop.__new__(WakeLoop)
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
    wl = WakeLoop.__new__(WakeLoop)
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
    wl = WakeLoop.__new__(WakeLoop)
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
