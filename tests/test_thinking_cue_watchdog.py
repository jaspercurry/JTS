from __future__ import annotations

import asyncio
import sys
import types

import pytest

# voice_daemon transitively imports several Pi-only audio deps
# (camilladsp, sounddevice, openwakeword). Stub them so the watchdog
# — a pure free function with no audio I/O — is importable on a
# vanilla laptop venv.
for _mod in ("camilladsp", "sounddevice", "openwakeword", "openwakeword.model"):
    sys.modules.setdefault(_mod, types.ModuleType(_mod))
sys.modules["camilladsp"].CamillaClient = object  # type: ignore[attr-defined]
sys.modules["openwakeword.model"].Model = object  # type: ignore[attr-defined]

from jasper.voice_daemon import _thinking_cue_watchdog  # noqa: E402


class _FakeTurn:
    """Minimal LiveTurn-shaped stub for the watchdog. Tests poke
    `chunk_at` and `lost` directly to simulate the relevant
    state transitions."""

    def __init__(self) -> None:
        self.chunk_at: float = 0.0
        self.lost: bool = False

    def last_chunk_at(self) -> float:
        return self.chunk_at

    def turn_lost(self) -> bool:
        return self.lost


@pytest.mark.asyncio
async def test_fires_cue_after_delay_when_no_chunk_arrives():
    turn = _FakeTurn()
    fired: list[bool] = []

    async def fire_cue() -> None:
        fired.append(True)

    # Pin input_ended_at to "now" so elapsed crosses the 0.05 s delay
    # almost immediately on the first poll.
    input_ended_at = asyncio.get_event_loop().time()

    await asyncio.wait_for(
        _thinking_cue_watchdog(
            turn, lambda: input_ended_at, fire_cue, delay_sec=0.05,
        ),
        timeout=2.0,
    )
    assert fired == [True]


@pytest.mark.asyncio
async def test_skips_cue_when_model_audio_arrives_first():
    turn = _FakeTurn()
    fired: list[bool] = []

    async def fire_cue() -> None:
        fired.append(True)

    input_ended_at = asyncio.get_event_loop().time()

    async def deliver_chunk_quickly() -> None:
        await asyncio.sleep(0.05)
        turn.chunk_at = asyncio.get_event_loop().time()

    chunk_task = asyncio.create_task(deliver_chunk_quickly())
    await asyncio.wait_for(
        _thinking_cue_watchdog(
            turn, lambda: input_ended_at, fire_cue, delay_sec=2.0,
        ),
        timeout=2.0,
    )
    await chunk_task
    assert fired == []


@pytest.mark.asyncio
async def test_waits_while_user_still_speaking():
    """While input_ended_at == 0.0 (user still talking), the watchdog
    must not fire even after `delay_sec` of wall-clock time. Once
    input_ended_at flips, the timer starts from THAT moment."""
    turn = _FakeTurn()
    fired: list[bool] = []

    async def fire_cue() -> None:
        fired.append(True)

    state = {"input_ended_at": 0.0}

    async def end_input_after_delay() -> None:
        # User keeps talking for 0.4 s (longer than delay_sec=0.05)
        # before the silence detector arms activity_end. The watchdog
        # must NOT have fired during the talking window.
        await asyncio.sleep(0.4)
        state["input_ended_at"] = asyncio.get_event_loop().time()

    end_input_task = asyncio.create_task(end_input_after_delay())
    await asyncio.wait_for(
        _thinking_cue_watchdog(
            turn,
            lambda: state["input_ended_at"],
            fire_cue,
            delay_sec=0.05,
        ),
        timeout=2.0,
    )
    await end_input_task
    # Cue should fire ONCE, after input_ended flipped + delay elapsed.
    assert fired == [True]


@pytest.mark.asyncio
async def test_exits_early_on_turn_lost():
    turn = _FakeTurn()
    fired: list[bool] = []

    async def fire_cue() -> None:
        fired.append(True)

    input_ended_at = asyncio.get_event_loop().time()

    async def lose_connection() -> None:
        await asyncio.sleep(0.05)
        turn.lost = True

    drop_task = asyncio.create_task(lose_connection())
    await asyncio.wait_for(
        _thinking_cue_watchdog(
            turn, lambda: input_ended_at, fire_cue, delay_sec=2.0,
        ),
        timeout=2.0,
    )
    await drop_task
    assert fired == []


@pytest.mark.asyncio
async def test_swallows_exceptions_from_fire_cue():
    """A failing cue (e.g. missing WAV file) must not crash the
    watchdog — silent failure of the cue is preferable to leaving
    a bg task in a faulted state for the rest of the turn."""
    turn = _FakeTurn()

    async def fire_cue() -> None:
        raise RuntimeError("simulated cue failure")

    input_ended_at = asyncio.get_event_loop().time()

    # Should complete normally — no exception propagates.
    await asyncio.wait_for(
        _thinking_cue_watchdog(
            turn, lambda: input_ended_at, fire_cue, delay_sec=0.05,
        ),
        timeout=2.0,
    )
