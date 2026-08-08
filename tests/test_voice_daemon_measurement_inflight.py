# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""In-flight audio drain on MEASURE_PAUSE (issue #1898).

#1786 stopped proactive cues, timers, and research announcements from
*starting* once a room-correction measurement window is open. This is its
residual half: `measurement_pause()` checked only `State.SESSION`, never
`_output_gate.is_active`, so a cue or timer announcement that began a
moment BEFORE the PAUSE landed kept playing into the window's first
capture.

`measurement_pause()` now arms the window first (so nothing new can start
and mic frames stop immediately) and then waits, bounded by
`MEASUREMENT_INFLIGHT_DRAIN_SEC`, for the already-playing episode to
finish. On timeout it proceeds with the window open and says so at
WARNING — it never blocks the measurement flow, and never fails open
silently.

The bound is a compatibility ceiling, not a preference: an OLD
coordinator (which awaits the reply with
`VOICE_MEASURE_PAUSE_TIMEOUT_SEC`) can be talking to a NEW daemon across
a deploy, and a coordinator that times out skips MEASURE_RESUME. The
pairing is pinned here.

The #1786 refusal behaviour itself lives in
tests/test_voice_daemon_measurement_gate.py; the gate's own idle-wait
primitive is unit-tested in tests/test_voice_output_gate.py.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile

from jasper.correction.coordinator import (
    MEASUREMENT_LEASE_REFRESH_SEC,
    MEASUREMENT_LEASE_RETRY_SEC,
    VOICE_MEASURE_PAUSE_TIMEOUT_SEC,
    _voice_uds_command,
)
from jasper.tts_routing import FANIN_TTS_SOCKET, OUTPUTD_TTS_SOCKET
from jasper.voice.output_gate import AssistantOutputGate
from jasper.voice_daemon import (
    MEASUREMENT_AUTOCLEAR_SEC,
    MEASUREMENT_INFLIGHT_DRAIN_SEC,
    State,
    WakeLoop,
)

from ._async_wait import wait_signalled


class _IdleGate:
    """Gate that is idle and must therefore never be waited on."""

    is_active = False
    active_kind = None

    async def wait_idle(self, timeout: float) -> bool:
        raise AssertionError(
            "measurement_pause must not wait when output is already idle"
        )


class _StuckGate:
    """Gate whose episode never ends — the drain-timeout path, with no
    real sleep: `wait_idle` reports failure immediately."""

    is_active = True
    active_kind = "proactive"

    def __init__(self) -> None:
        self.waits: list[float] = []

    async def wait_idle(self, timeout: float) -> bool:
        self.waits.append(timeout)
        return False


class _ObservedGate(AssistantOutputGate):
    """The real gate, announcing when the drain starts waiting on it.

    Lets a test tell "the reply is held INSIDE the drain" apart from
    "the reply has not crossed the socket yet" — the two look identical
    from the client side, and only the first is what #1898 fixed.
    """

    def __init__(self) -> None:
        super().__init__()
        self.drain_entered = asyncio.Event()

    async def wait_idle(self, timeout: float) -> bool:
        self.drain_entered.set()
        return await super().wait_idle(timeout)


class _RefusingCues:
    """Cue manager that raises if asked to play — proves nothing played."""

    async def play(self, _slug: str) -> bool:
        raise AssertionError("cue must not play during a measurement window")


async def _close_window(wl: WakeLoop) -> None:
    """Cancel the auto-clear safety task so the test loop closes clean."""
    await wl.measurement_resume()


# --- the defect: an in-flight cue delays the window ------------------------


async def test_pause_waits_for_inflight_cue_then_returns() -> None:
    wl = WakeLoop.for_tests()
    episode = await wl._output_gate.begin_if_idle("admin")
    assert episode is not None

    pause = asyncio.create_task(wl.measurement_pause())
    for _ in range(5):
        await asyncio.sleep(0)

    # Still draining: the reply the coordinator awaits has not been sent,
    # so it has not logged "measurement window OPEN" or armed a capture.
    assert not pause.done()

    await wl._output_gate.end(episode)

    assert await asyncio.wait_for(pause, timeout=1.0) == "ok"
    assert wl._measurement_active.is_set()
    await _close_window(wl)


async def test_pause_drains_tts_for_both_supported_mix_stages() -> None:
    """The voice gate is route-independent: it covers fan-in and outputd TTS."""

    for tts_socket in (FANIN_TTS_SOCKET, OUTPUTD_TTS_SOCKET):
        wl = WakeLoop.for_tests()
        wl._cfg.tts_outputd_socket = tts_socket
        episode = await wl._output_gate.begin_if_idle("assistant")
        assert episode is not None

        pause = asyncio.create_task(wl.measurement_pause())
        for _ in range(5):
            await asyncio.sleep(0)

        assert wl._measurement_active.is_set()
        assert not pause.done(), tts_socket
        await wl._output_gate.end(episode)
        assert await asyncio.wait_for(pause, timeout=1.0) == "ok"
        await _close_window(wl)


async def test_window_is_armed_before_the_drain_not_after() -> None:
    """Ordering is load-bearing: the flag every #1786 entry point reads
    (and the mic-frame gate) must already be set while we drain, or a new
    cue could start — or a wake could fire — inside the wait."""
    wl = WakeLoop.for_tests()
    wl._cues = _RefusingCues()
    episode = await wl._output_gate.begin_if_idle("proactive")
    assert episode is not None

    pause = asyncio.create_task(wl.measurement_pause())
    for _ in range(5):
        await asyncio.sleep(0)

    assert not pause.done()
    assert wl._measurement_active.is_set()
    # A crash mid-drain still self-heals: the safety timer is already armed.
    assert wl._measurement_safety_task is not None
    assert not wl._measurement_safety_task.done()
    # And the pre-existing #1786 gate is live during the wait.
    assert await wl.play_cue("cant_connect") == "measurement_active"

    await wl._output_gate.end(episode)
    assert await asyncio.wait_for(pause, timeout=1.0) == "ok"
    await _close_window(wl)


async def test_drain_defers_to_inflight_audio_and_never_cancels_it() -> None:
    """The pause waits the episode out; it does not end it. A
    wake-blocking cue already speaking is finished, not cut short."""
    wl = WakeLoop.for_tests()
    episode = await wl._output_gate.begin_if_idle("admin")
    assert episode is not None

    pause = asyncio.create_task(wl.measurement_pause())
    for _ in range(5):
        await asyncio.sleep(0)

    # Still waiting, and the episode still owns output — nobody revoked it.
    assert not pause.done()
    assert wl._output_gate.is_active
    assert wl._output_gate.is_current(episode)

    await wl._output_gate.end(episode)
    assert await asyncio.wait_for(pause, timeout=1.0) == "ok"
    await _close_window(wl)


async def test_drained_path_logs_the_wait(caplog) -> None:
    wl = WakeLoop.for_tests()
    episode = await wl._output_gate.begin_if_idle("admin")
    assert episode is not None

    with caplog.at_level(logging.INFO, logger="jasper.voice_daemon"):
        pause = asyncio.create_task(wl.measurement_pause())
        for _ in range(5):
            await asyncio.sleep(0)
        await wl._output_gate.end(episode)
        assert await asyncio.wait_for(pause, timeout=1.0) == "ok"

    assert "event=measurement.inflight_drained" in caplog.text
    assert "active_kind=admin" in caplog.text
    await _close_window(wl)


# --- the bound: proceed on timeout, loudly ---------------------------------


async def test_pause_proceeds_with_warning_when_the_drain_times_out(
    caplog,
) -> None:
    wl = WakeLoop.for_tests()
    gate = _StuckGate()
    wl._output_gate = gate

    with caplog.at_level(logging.INFO, logger="jasper.voice_daemon"):
        result = await wl.measurement_pause()

    # Never block the measurement flow.
    assert result == "ok"
    assert wl._measurement_active.is_set()
    # Never fail the window open silently.
    assert "event=measurement.inflight_drain_timeout" in caplog.text
    assert "active_kind=proactive" in caplog.text
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and "measurement.inflight_drain_timeout" in r.getMessage()
    ]
    assert len(warnings) == 1
    # Waited exactly the module bound, once.
    assert gate.waits == [MEASUREMENT_INFLIGHT_DRAIN_SEC]

    await _close_window(wl)


# --- the common path: idle gate costs nothing ------------------------------


async def test_idle_output_never_waits() -> None:
    wl = WakeLoop.for_tests()
    wl._output_gate = _IdleGate()

    assert await wl.measurement_pause() == "ok"
    assert wl._measurement_active.is_set()
    await _close_window(wl)


async def test_lease_refresh_into_an_open_window_never_waits() -> None:
    """The coordinator re-sends MEASURE_PAUSE every
    MEASUREMENT_LEASE_REFRESH_SEC to renew the daemon's crash-recovery
    lease. The first capture began long ago and
    #1786 blocks new output, so a renewal has nothing to drain and must
    stay latency-free even if something is somehow playing."""
    wl = WakeLoop.for_tests()
    assert await wl.measurement_pause() == "ok"

    gate = _StuckGate()
    wl._output_gate = gate
    assert await wl.measurement_pause() == "ok"

    assert gate.waits == []
    await _close_window(wl)


async def test_active_session_still_refuses_without_draining() -> None:
    wl = WakeLoop.for_tests()
    wl._state = State.SESSION
    gate = _StuckGate()
    wl._output_gate = gate

    assert await wl.measurement_pause() == "BUSY"
    assert not wl._measurement_active.is_set()
    assert gate.waits == []


# --- protocol compatibility, both deploy directions ------------------------


def test_drain_bound_fits_under_the_coordinator_read_timeout() -> None:
    """OLD coordinator + NEW daemon. install.sh restarts jasper-voice and
    jasper-web at different points of a deploy, so a coordinator pinned to
    the timeout it shipped with can be awaiting a daemon that now holds
    the reply through a drain. A coordinator that gives up believes voice
    was never paused, skips MEASURE_RESUME, and leaves the speaker gated
    until the daemon's auto-clear — so the bound must fit under the
    timeout with room for connect/write/scheduling on a loaded Pi."""
    assert MEASUREMENT_INFLIGHT_DRAIN_SEC < VOICE_MEASURE_PAUSE_TIMEOUT_SEC
    assert (
        VOICE_MEASURE_PAUSE_TIMEOUT_SEC - MEASUREMENT_INFLIGHT_DRAIN_SEC >= 1.0
    )


def test_lease_refresh_fits_under_the_daemon_measurement_auto_clear() -> None:
    """The other half of the same window's timing contract. The daemon
    auto-clears a measurement window it has not heard about in
    MEASUREMENT_AUTOCLEAR_SEC, which is the crash backstop; the coordinator
    keeps a legitimate long window alive by re-sending MEASURE_PAUSE every
    MEASUREMENT_LEASE_REFRESH_SEC. Invert that order and a healthy sweep
    un-gates itself mid-capture and lets household music back in.

    The budget is a whole failed-renewal cycle, because a failure costs far
    more than the retry delay. The failed attempt waits up to
    VOICE_MEASURE_PAUSE_TIMEOUT_SEC for a reply BEFORE the loop switches to
    the retry interval, and the retry then waits that long again — see
    `_refresh_voice_lease` in correction/coordinator.py. Budget only the retry
    delay and you green-light a refresh interval whose retry is still in
    flight when the backstop fires."""
    assert 0 < MEASUREMENT_LEASE_REFRESH_SEC < MEASUREMENT_AUTOCLEAR_SEC
    assert (
        MEASUREMENT_LEASE_REFRESH_SEC
        + 2 * VOICE_MEASURE_PAUSE_TIMEOUT_SEC
        + MEASUREMENT_LEASE_RETRY_SEC
        < MEASUREMENT_AUTOCLEAR_SEC
    )


async def test_old_coordinator_read_timeout_survives_a_held_reply() -> None:
    """Both directions on the wire, end to end through the real socket.

    An OLD coordinator, reading with the timeout it shipped with, still
    gets its reply when a NEW daemon holds that reply through a drain —
    so it still sets `voice_paused` and still sends MEASURE_RESUME. And
    the reply gains no new required field, so a NEW coordinator reading
    an OLD daemon's `{"result": "ok"}` is equally unchanged.

    The pending-ness is pinned against the drain itself, not against
    elapsed time: `_voice_uds_command` is in flight for a moment
    regardless, so a plain "not done yet" would pass even with no drain
    at all.
    """
    from jasper.voice.daemon_main import _start_control_socket

    wl = WakeLoop.for_tests()
    gate = _ObservedGate()
    wl._output_gate = gate
    # Not tmp_path: AF_UNIX paths cap at ~104 bytes and a worktree-rooted
    # pytest tmpdir overruns it.
    sock_dir = tempfile.mkdtemp(dir="/tmp", prefix="jts-uds-")
    socket_path = f"{sock_dir}/voice.sock"
    server = await _start_control_socket(wl, socket_path)
    try:
        episode = await gate.begin_if_idle("admin")
        assert episode is not None

        request = asyncio.create_task(
            _voice_uds_command(
                socket_path,
                "MEASURE_PAUSE",
                timeout=VOICE_MEASURE_PAUSE_TIMEOUT_SEC,
            )
        )
        # The handler has reached the drain: the reply is being held
        # there, not merely still in transit.
        await wait_signalled(
            gate.drain_entered,
            "MEASURE_PAUSE drain start",
            producer=request,
        )
        assert not request.done()

        await gate.end(episode)
        # The inner read is bounded by the coordinator's own timeout, so
        # a daemon that held too long fails here as that TimeoutError.
        resp = await asyncio.wait_for(
            request, timeout=VOICE_MEASURE_PAUSE_TIMEOUT_SEC + 1.0,
        )

        # Exactly what the pre-#1898 coordinator branches on.
        assert resp.get("result") == "ok"
        assert resp == {"result": "ok"}
    finally:
        server.close()
        await server.wait_closed()
        await _close_window(wl)
        shutil.rmtree(sock_dir, ignore_errors=True)
