# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for push-to-talk-only speakers: the zero-leg run() keepalive/
heartbeat/teardown path (#2205). The zero-wake-leg derivation itself is
pinned directly against `PushToTalk` in tests/test_push_to_talk.py."""
from __future__ import annotations

from tests._log_events import event_fields


def test_zero_leg_wakeloop_has_no_primary_mic_or_detector():
    """The primary-leg aliases must tolerate the absent "on" leg. `_mic` is
    what run() branches on; `_capture_ring_on` must still be a real deque so
    its readers need no special case."""
    from collections import deque

    from jasper.voice.push_to_talk import ManualMicRuntime
    from jasper.voice_daemon import WakeLoop

    wl = WakeLoop.for_tests(
        legs=[],
        manual_mics=[ManualMicRuntime("wiim_remote_2", object(), "udp:9892")],
    )
    assert wl._mic is None
    assert wl._detector is None
    assert isinstance(wl._capture_ring_on, deque)


def _zero_leg_loop_with_fast_keepalive(monkeypatch):
    """A PTT-only WakeLoop whose keepalive iterates promptly, plus a
    heartbeat spy. The cadence itself is pinned by
    test_ptt_keepalive_stays_inside_heartbeat_stale_threshold in
    tests/test_push_to_talk.py; here we only need the loop to turn over
    quickly."""
    import asyncio

    from jasper.voice.push_to_talk import ManualMicRuntime
    from jasper.voice_daemon import WakeLoop

    class _IdleMic:
        """A paired remote with its button not pressed — the steady state.

        Sends nothing and never ends, which is exactly why frame flow cannot
        prove an accessory is alive (issue #2243)."""

        async def frames(self):
            await asyncio.Event().wait()
            yield b""  # unreachable; keeps this an async generator

    wl = WakeLoop.for_tests(
        legs=[],
        manual_mics=[
            ManualMicRuntime("wiim_remote_2", _IdleMic(), "udp:9892"),
        ],
    )
    ticked = asyncio.Event()
    bumps = []

    class _Heartbeat:
        def bump(self):
            bumps.append(1)
            ticked.set()

    wl._heartbeat = _Heartbeat()

    real_sleep = asyncio.sleep

    async def _fast_sleep(_seconds):
        # Still yields to the event loop — a bare `return None` would let the
        # keepalive spin without ever suspending and starve the test.
        await real_sleep(0)

    monkeypatch.setattr("jasper.voice.push_to_talk.asyncio.sleep", _fast_sleep)
    return wl, ticked, bumps


async def test_zero_leg_run_ticks_the_heartbeat_without_a_primary_mic(
    monkeypatch, caplog,
):
    """run() must keep the Tier-1 heartbeat alive on a speaker with no
    primary mic, and must not mistake a tick for audio.

    Without the keepalive the heartbeat's progress sentinel never advances,
    the thread stops patting systemd, and WatchdogSec=30s restarts a daemon
    that is working exactly as designed.
    """
    import asyncio
    import logging

    wl, ticked, bumps = _zero_leg_loop_with_fast_keepalive(monkeypatch)
    # A list, not a deque: if a tick ever reached the frame body it would be
    # appended here and the assertion below would see it.
    wl._pre_roll = []

    with caplog.at_level(logging.INFO, logger="jasper.voice_daemon"):
        task = asyncio.create_task(wl.run())
        await asyncio.wait_for(ticked.wait(), timeout=2.0)
        wl._stop_event.set()
        await asyncio.wait_for(task, timeout=2.0)

    assert bumps
    assert wl._pre_roll == []
    # The mode announces itself once, by this exact name: it is what the
    # owed #2205 hardware run greps for in the journal to confirm the box
    # came up push-to-talk rather than silently mic-less.
    fields = event_fields(caplog, "voice.push_to_talk_only")
    assert fields["sources"] == "wiim_remote_2"


async def test_zero_leg_run_ends_an_in_flight_turn_on_stop(monkeypatch):
    """SIGTERM mid-hold must still tear the turn down.

    `run()`'s stop branch is what calls `_end_turn` — duck restore,
    `end_input`, turn telemetry, the done-listening chirp. When the keepalive
    generator carried its OWN `_stop_event` check it ended the iteration
    first, so that branch was unreachable on this path and a stop during a
    button hold left the music ducked and the turn unfinished. The generator
    ticks unconditionally now; the consumer owns shutdown, exactly as it does
    for a real mic's frames().
    """
    import asyncio

    from jasper.voice_daemon import State

    wl, ticked, _bumps = _zero_leg_loop_with_fast_keepalive(monkeypatch)
    ended = asyncio.Event()
    reasons: list[str] = []

    async def _end_turn(reason: str = "ended"):
        reasons.append(reason)
        ended.set()

    wl._end_turn = _end_turn
    wl._state = State.SESSION  # a button turn is in flight

    task = asyncio.create_task(wl.run())
    await asyncio.wait_for(ticked.wait(), timeout=2.0)
    wl._stop_event.set()
    await asyncio.wait_for(task, timeout=2.0)

    assert ended.is_set(), (
        "stop during an in-flight turn must reach run()'s _end_turn branch"
    )
    # The reason a shutdown teardown gives itself: a turn with no answer
    # owes no failure cue when the daemon is the one going away.
    assert reasons == ["stopping"]


async def test_zero_leg_run_does_not_end_a_turn_that_is_not_running(
    monkeypatch,
):
    """Control for the test above: idle at stop → no teardown, so the
    assertion there is about the SESSION branch and not about `run()`
    calling `_end_turn` unconditionally on every shutdown."""
    import asyncio

    wl, ticked, _bumps = _zero_leg_loop_with_fast_keepalive(monkeypatch)
    calls = []

    async def _end_turn(reason: str = "ended"):
        calls.append(reason)

    wl._end_turn = _end_turn

    task = asyncio.create_task(wl.run())
    await asyncio.wait_for(ticked.wait(), timeout=2.0)
    wl._stop_event.set()
    await asyncio.wait_for(task, timeout=2.0)

    assert calls == []
