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

    async def __call__(self, *args, **kwargs) -> None:
        self.called = True


def _make_wake_loop():
    """A WakeLoop with only the attributes manual_session_start reads
    plus spies on the side effects we assert must NOT fire.
    """
    from jasper.voice_daemon import State, WakeLoop

    wl = WakeLoop.__new__(WakeLoop)
    wl._state = State.WAKE
    wl._mic_muted = False
    wl._measurement_active = asyncio.Event()
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
    assert "reason=measurement" in caplog.text


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
    assert wl._prepare_assistant_loudness_context.called is True
    assert wl._play_listening_chirp.called is True
