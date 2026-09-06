# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for push-to-talk-only speakers: the zero-wake-leg derivation
and the zero-leg run() keepalive/heartbeat/teardown path (#2205)."""
from __future__ import annotations

from tests._live_turn_fake import _prep_session_status


def _remote_runtime():
    from jasper.voice_daemon import _ManualMicRuntime
    return [_ManualMicRuntime("wiim_remote_2", object(), "udp:9892")]


def test_push_to_talk_only_is_derived_from_resolved_runtime():
    """The daemon knows it is push-to-talk from what it actually opened —
    zero wake legs plus at least one manual mic source — never from a config
    string it might have inherited from a default."""
    from jasper.voice_daemon import WakeLoop

    assert WakeLoop.for_tests(
        legs=[], manual_mics=_remote_runtime(),
    )._push_to_talk_only is True
    # Zero legs and no manual source is a broken speaker, not a PTT one.
    assert WakeLoop.for_tests(legs=[])._push_to_talk_only is False
    # A remote on a speaker that also has a room mic is additive.
    assert WakeLoop.for_tests(
        manual_mics=_remote_runtime(),
    )._push_to_talk_only is False


def test_push_to_talk_only_is_the_single_derivation_its_consumers_read():
    """One fact, one derivation, and the sites that act on it read THAT.

    A field with a producer and no consumer is a claim nothing enforces. The
    two acting sites used to re-derive the mode from `self._mic is None`
    independently; forcing the field is now enough to move both, which is
    what makes it the owner of the fact rather than a parallel copy of it.
    """
    from jasper.voice_daemon import WakeLoop

    wl = WakeLoop.for_tests(legs=[], manual_mics=_remote_runtime())
    _prep_session_status(wl)

    # /state, via session_status(). This is the observability half: an empty
    # `wake_legs` alone cannot tell "arms nothing on purpose" from "every leg
    # failed to open" — opposite diagnoses that render identically without it.
    status = wl.session_status()
    assert status["push_to_talk_only"] is True
    assert status["wake_legs"] == []
    assert status["manual_mic_sources"] == ["wiim_remote_2"]

    # A speaker WITH a room mic reports the mode off.
    other = WakeLoop.for_tests(manual_mics=_remote_runtime())
    _prep_session_status(other)
    assert other.session_status()["push_to_talk_only"] is False
    # The other two consumers — run()'s keepalive branch and the source-less
    # start refusal — are pinned by
    # test_zero_leg_run_ticks_the_heartbeat_without_a_primary_mic below and by
    # test_source_less_refusal_reads_the_single_derivation in
    # tests/test_voice_daemon_manual_start_guard.py, both of which move when
    # this one field moves.


def test_zero_leg_wakeloop_has_no_primary_mic_or_detector():
    """The primary-leg aliases must tolerate the absent "on" leg. `_mic` is
    what run() branches on; `_capture_ring_on` must still be a real deque so
    its readers need no special case."""
    from collections import deque

    from jasper.voice_daemon import _ManualMicRuntime, WakeLoop

    wl = WakeLoop.for_tests(
        legs=[],
        manual_mics=[_ManualMicRuntime("wiim_remote_2", object(), "udp:9892")],
    )
    assert wl._mic is None
    assert wl._detector is None
    assert isinstance(wl._capture_ring_on, deque)


def _daemon_heartbeat_stale_threshold() -> float:
    """The stale threshold the DAEMON actually runs with.

    Read from `jasper/voice/daemon_main.py`'s own `Heartbeat(...)` call, not
    from the constructor's signature default: those two happen to be the same
    number today, so a guard that read the signature would be correct only by
    coincidence and would keep passing if the daemon started asking for a
    tighter threshold. Parsed with `ast` rather than by line number so a
    refactor moves it for free (AGENTS.md documentation rule 5).
    """
    import ast
    import inspect
    from pathlib import Path

    import jasper
    from jasper.watchdog import Heartbeat

    source = (
        Path(jasper.__file__).parent / "voice" / "daemon_main.py"
    ).read_text(encoding="utf-8")
    calls = [
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Heartbeat"
    ]
    assert len(calls) == 1, (
        f"expected exactly one Heartbeat(...) construction in daemon_main.py, "
        f"found {len(calls)} — this guard must read the live one"
    )
    for kw in calls[0].keywords:
        if kw.arg == "stale_threshold_sec":
            return float(ast.literal_eval(kw.value))
    # No explicit value: the daemon runs on the constructor default.
    return float(
        inspect.signature(Heartbeat).parameters["stale_threshold_sec"].default
    )


def test_ptt_keepalive_stays_inside_heartbeat_stale_threshold():
    """Load-bearing relationship: with no mic frames to bump the progress
    sentinel, the keepalive tick IS the liveness proof. If its interval ever
    drifts past the threshold the daemon asks for, the heartbeat thread stops
    patting systemd and WatchdogSec=30s reaps a perfectly healthy daemon."""
    from jasper.voice_daemon import PTT_KEEPALIVE_INTERVAL_SEC

    stale = _daemon_heartbeat_stale_threshold()
    assert PTT_KEEPALIVE_INTERVAL_SEC < stale, (
        f"keepalive {PTT_KEEPALIVE_INTERVAL_SEC}s must stay under the "
        f"{stale}s heartbeat stale threshold jasper-voice constructs with"
    )


def _zero_leg_loop_with_fast_keepalive(monkeypatch):
    """A PTT-only WakeLoop whose keepalive iterates promptly, plus a
    heartbeat spy. The cadence itself is pinned by the threshold test above;
    here we only need the loop to turn over quickly."""
    import asyncio

    from jasper.voice_daemon import _ManualMicRuntime, WakeLoop

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
            _ManualMicRuntime("wiim_remote_2", _IdleMic(), "udp:9892"),
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

    monkeypatch.setattr("jasper.voice_daemon.asyncio.sleep", _fast_sleep)
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
    assert "event=voice.push_to_talk_only" in caplog.text
    assert "sources=wiim_remote_2" in caplog.text


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
