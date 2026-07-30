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
    VOICE_MEASURE_PAUSE_TIMEOUT_SEC,
    _voice_uds_command,
)
from jasper.voice_daemon import MEASUREMENT_INFLIGHT_DRAIN_SEC, State, WakeLoop


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


class _RefusingCues:
    """Cue manager that raises if asked to play — proves nothing played."""

    async def play(self, _slug: str) -> bool:
        raise AssertionError("cue must not play during a measurement window")


async def _close_window(wl: WakeLoop) -> None:
    """Cancel the 2-minute safety task so the test loop closes clean."""
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
    """The coordinator re-sends MEASURE_PAUSE every 60 s to renew the
    daemon's crash-recovery lease. The first capture began long ago and
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
    until the 2-minute auto-clear — so the bound must fit under the
    timeout with room for connect/write/scheduling on a loaded Pi."""
    assert MEASUREMENT_INFLIGHT_DRAIN_SEC < VOICE_MEASURE_PAUSE_TIMEOUT_SEC
    assert (
        VOICE_MEASURE_PAUSE_TIMEOUT_SEC - MEASUREMENT_INFLIGHT_DRAIN_SEC >= 1.0
    )


async def test_old_coordinator_wire_shape_survives_a_drain() -> None:
    """Both directions on the wire. The reply gains no new required
    field, so a NEW coordinator reading an OLD daemon's `{"result": "ok"}`
    is unchanged; and an OLD coordinator's own read predicate still
    matches when a NEW daemon answers late, from behind a drain."""
    from jasper.voice.daemon_main import _start_control_socket

    wl = WakeLoop.for_tests()
    # Not tmp_path: AF_UNIX paths cap at ~104 bytes and a worktree-rooted
    # pytest tmpdir overruns it.
    sock_dir = tempfile.mkdtemp(dir="/tmp", prefix="jts-uds-")
    socket_path = f"{sock_dir}/voice.sock"
    server = await _start_control_socket(wl, socket_path)
    try:
        episode = await wl._output_gate.begin_if_idle("admin")
        assert episode is not None

        request = asyncio.create_task(
            _voice_uds_command(
                socket_path,
                "MEASURE_PAUSE",
                timeout=VOICE_MEASURE_PAUSE_TIMEOUT_SEC,
            )
        )
        for _ in range(5):
            await asyncio.sleep(0)
        assert not request.done()

        await wl._output_gate.end(episode)
        resp = await asyncio.wait_for(request, timeout=2.0)

        # Exactly what the pre-#1898 coordinator branches on.
        assert resp.get("result") == "ok"
        assert resp == {"result": "ok"}
    finally:
        server.close()
        await server.wait_closed()
        await _close_window(wl)
        shutil.rmtree(sock_dir, ignore_errors=True)
