# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""manual_session_start must honor the user-deliberate stop-listening
gates the wake path enforces.

The dial long-press / POST /session/start entry opens a paid LLM turn
and ducks music via _begin_turn. The wake path refuses to do that when
the mic is muted or a room-correction measurement sweep is active
(_wake_late_cancelled). These tests pin that manual_session_start does
the same: it returns a refusal code, logs event=session.manual_refused,
and never plays a chirp, primes loudness context, or begins a turn (so
no duck) under either gate.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import types


# Same import-time stubs the sibling voice_daemon tests use so importing
# jasper.voice_daemon doesn't require the hardware-only deps.
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
    wl._connection = types.SimpleNamespace(is_paused=lambda: False)
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


async def test_manual_start_begins_turn_when_unguarded():
    # Control: with both stop-listening gates clear (and spend/connection
    # allowed), manual_session_start proceeds normally — proving the new
    # guard is scoped to the muted/measuring conditions only.
    wl = _make_wake_loop()

    result = await wl.manual_session_start()
    # The listening chirp is fire-and-forget (create_task); drain
    # pending tasks so it runs and doesn't leak a never-awaited coro.
    await asyncio.gather(
        *(t for t in asyncio.all_tasks() if t is not asyncio.current_task())
    )

    assert result == "OK"
    assert wl._begin_turn.called is True
    assert wl._begin_turn.kwargs == {}
    assert wl._prepare_assistant_loudness_context.called is True
    assert wl._play_listening_chirp.called is True


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
    await asyncio.gather(
        *(t for t in asyncio.all_tasks() if t is not asyncio.current_task())
    )

    assert result == "OK"
    assert wl._begin_turn.called is True
    assert wl._begin_turn.kwargs == {"pre_roll": False}
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
