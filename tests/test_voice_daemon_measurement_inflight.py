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

`measurement_pause()` now closes output admission atomically, arms the window
(so nothing new can start and mic frames stop immediately), and then waits,
bounded by
`MEASUREMENT_INFLIGHT_DRAIN_SEC`, for the already-playing episode to
finish. On timeout it preserves the compatible `result=ok`, adds
`drained=false`, and keeps cleanup ownership armed; strict callers refuse to
capture, while the historical permissive correction path may proceed but must
still send RESUME.

The bound is a compatibility ceiling, not a preference: an OLD coordinator
(which awaits the reply with `VOICE_MEASURE_PAUSE_TIMEOUT_SEC`) can be talking
to a NEW daemon across a deploy. A transport timeout still leaves that old
caller unable to know the pause was armed, so the auto-clear is the backstop;
current callers additionally read the additive `drained` evidence and retain
cleanup ownership. The pairing is pinned here.

The #1786 refusal behaviour itself lives in
tests/test_voice_daemon_measurement_gate.py; the gate's own idle-wait
primitive is unit-tested in tests/test_voice_output_gate.py.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import threading

import pytest

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

    def __init__(self) -> None:
        self.admission_paused = False

    async def pause_admission(self) -> bool:
        changed = not self.admission_paused
        self.admission_paused = True
        return changed

    async def drain_paused(self, timeout: float) -> bool:
        del timeout
        return True

    async def resume_admission(self) -> bool:
        changed = self.admission_paused
        self.admission_paused = False
        return changed

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
        self.admission_paused = False

    async def pause_admission(self) -> bool:
        changed = not self.admission_paused
        self.admission_paused = True
        return changed

    async def drain_paused(self, timeout: float) -> bool:
        self.waits.append(timeout)
        return False

    async def resume_admission(self) -> bool:
        changed = self.admission_paused
        self.admission_paused = False
        return changed

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

    async def drain_paused(self, timeout: float) -> bool:
        self.drain_entered.set()
        return await super().drain_paused(timeout)


class _CountingGate(AssistantOutputGate):
    """Real gate with observable cleanup calls for error-path ownership."""

    def __init__(self) -> None:
        super().__init__()
        self.resume_calls = 0

    async def resume_admission(self) -> bool:
        self.resume_calls += 1
        return await super().resume_admission()


class _TailHeldTts:
    """TTS fake whose write returns before its physical tail drains."""

    def __init__(self) -> None:
        self.drain_started = asyncio.Event()
        self.release_drain = asyncio.Event()

    async def prepare_assistant_context(self, **_kwargs) -> None:
        return None

    async def write_segment(self, *_args, **_kwargs) -> None:
        return None

    async def wait_drained(self) -> None:
        self.drain_started.set()
        await self.release_drain.wait()

    async def pause_content_meter(self) -> None:
        return None

    async def resume_content_meter(self) -> None:
        return None


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
        episode = await wl._output_gate.begin_turn()

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


# --- the bound: explicit drain evidence, cleanup remains owned --------------


async def test_pause_reports_additive_timeout_and_retains_cleanup(
    caplog,
) -> None:
    wl = WakeLoop.for_tests()
    gate = _StuckGate()
    wl._output_gate = gate

    with caplog.at_level(logging.INFO, logger="jasper.voice_daemon"):
        result, drained = await wl._measurement_pause_detailed()

    assert result == "ok"
    assert drained is False
    assert wl._measurement_active.is_set()
    assert gate.admission_paused
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
    assert not gate.admission_paused


# --- the common path: idle gate costs nothing ------------------------------


async def test_idle_output_never_waits() -> None:
    wl = WakeLoop.for_tests()
    wl._output_gate = _IdleGate()

    assert await wl.measurement_pause() == "ok"
    assert wl._measurement_active.is_set()
    await _close_window(wl)


async def test_pause_setup_error_restores_output_admission_once() -> None:
    class _FailingPauseTts:
        def __init__(self) -> None:
            self.resume_calls = 0

        async def pause_content_meter(self) -> None:
            raise RuntimeError("meter pause failed")

        async def resume_content_meter(self) -> None:
            self.resume_calls += 1

    wl = WakeLoop.for_tests()
    gate = _CountingGate()
    tts = _FailingPauseTts()
    wl._output_gate = gate
    wl._tts = tts

    with pytest.raises(RuntimeError, match="meter pause failed"):
        await wl.measurement_pause()

    assert not wl._measurement_active.is_set()
    assert not gate.admission_paused
    assert gate.resume_calls == 1
    assert tts.resume_calls == 1


@pytest.mark.parametrize("tts_socket", [FANIN_TTS_SOCKET, OUTPUTD_TTS_SOCKET])
async def test_pause_waits_for_physical_mute_click_tail(tts_socket: str) -> None:
    wl = WakeLoop.for_tests()
    wl._cfg.tts_outputd_socket = tts_socket
    tts = _TailHeldTts()
    wl._tts = tts

    click = asyncio.create_task(wl._play_mute_click(going_on=True))
    await wait_signalled(
        tts.drain_started,
        "mute click physical drain",
        producer=click,
    )

    pause = asyncio.create_task(wl.measurement_pause())
    for _ in range(5):
        await asyncio.sleep(0)
    assert not pause.done(), tts_socket
    assert wl._output_gate.active_kind == "feedback"

    tts.release_drain.set()
    await click
    assert await asyncio.wait_for(pause, timeout=1.0) == "ok"
    await _close_window(wl)


@pytest.mark.parametrize("tts_socket", [FANIN_TTS_SOCKET, OUTPUTD_TTS_SOCKET])
async def test_partial_mute_write_keeps_gate_until_accepted_prefix_drains(
    monkeypatch,
    tts_socket: str,
) -> None:
    """A later AUDIO failure cannot erase an earlier command's audible tail."""
    import scipy.signal

    import jasper.audio_io as audio_io_mod
    from jasper.audio_io import OutputdTtsPlayout

    class _FailSecondWrite:
        def __init__(self) -> None:
            self.attempts = 0

        def set_gain_db(self, _db: float) -> None:
            return None

        def start_segment(self, **_kwargs) -> None:
            return None

        def write(self, _data: bytes) -> None:
            self.attempts += 1
            if self.attempts == 2:
                raise OSError("second AUDIO command failed")

        def pause_content_meter(self) -> None:
            return None

        def resume_content_meter(self) -> None:
            return None

    monkeypatch.setattr(audio_io_mod, "_OUTPUTD_MAX_AUDIO_CHUNK_BYTES", 8)
    monkeypatch.setattr(
        scipy.signal,
        "resample_poly",
        lambda arr, *, up, down: arr,
    )
    drain_started = asyncio.Event()
    release_drain = asyncio.Event()

    async def fake_drain_sleep(seconds: float) -> None:
        assert seconds > 0
        drain_started.set()
        await release_drain.wait()

    fake_asyncio = type(
        "_FakeAsyncio",
        (),
        {
            "CancelledError": asyncio.CancelledError,
            "create_task": staticmethod(asyncio.create_task),
            "current_task": staticmethod(asyncio.current_task),
            "shield": staticmethod(asyncio.shield),
            "to_thread": staticmethod(asyncio.to_thread),
            "sleep": staticmethod(fake_drain_sleep),
        },
    )
    monkeypatch.setattr(audio_io_mod, "asyncio", fake_asyncio)

    tts = OutputdTtsPlayout(
        socket_path=tts_socket,
        output_rate=48000,
        gain_db=-8.0,
        drain_tail_sec=1.0,
    )
    stream = _FailSecondWrite()
    tts._stream = stream  # type: ignore[assignment]
    wl = WakeLoop.for_tests()
    wl._cfg.tts_outputd_socket = tts_socket
    wl._tts = tts
    wl._mute_click_on_pcm = b"\x01\x00" * 5

    click = asyncio.create_task(wl._play_mute_click(going_on=True))
    await wait_signalled(
        drain_started,
        "partial mute write accepted-prefix drain",
        producer=click,
    )
    assert stream.attempts == 2
    assert wl._output_gate.active_kind == "feedback"

    pause = asyncio.create_task(wl.measurement_pause_response())
    for _ in range(5):
        await asyncio.sleep(0)
    assert not pause.done(), tts_socket

    release_drain.set()
    await click
    assert await asyncio.wait_for(pause, timeout=1.0) == {
        "result": "ok",
        "drained": True,
    }
    await _close_window(wl)


@pytest.mark.parametrize("tts_socket", [FANIN_TTS_SOCKET, OUTPUTD_TTS_SOCKET])
async def test_cancelled_mute_write_waits_for_acceptance_and_physical_tail(
    tts_socket: str,
) -> None:
    """Cancellation cannot outrun an uncancellable socket-write worker."""
    from jasper.audio_io import OutputdTtsPlayout

    write_started = threading.Event()
    release_write = threading.Event()
    write_returned = threading.Event()

    class _BlockingWrite:
        def set_gain_db(self, _db: float) -> None:
            return None

        def start_segment(self, **_kwargs) -> None:
            return None

        def write(self, _data: bytes) -> None:
            write_started.set()
            if not release_write.wait(timeout=2.0):
                raise TimeoutError("test did not release AUDIO write")
            write_returned.set()

        def pause_content_meter(self) -> None:
            return None

        def resume_content_meter(self) -> None:
            return None

    tts = OutputdTtsPlayout(
        socket_path=tts_socket,
        output_rate=48000,
        gain_db=-8.0,
        drain_tail_sec=1.0,
    )
    tts._stream = _BlockingWrite()  # type: ignore[assignment]
    wl = WakeLoop.for_tests()
    wl._cfg.tts_outputd_socket = tts_socket
    wl._tts = tts
    wl._mute_click_on_pcm = b"\x01\x00" * 5

    click = asyncio.create_task(wl._play_mute_click(going_on=True))
    assert await asyncio.to_thread(write_started.wait, 1.0)
    click.cancel()
    for _ in range(5):
        await asyncio.sleep(0)
    assert not click.done(), tts_socket
    assert wl._output_gate.active_kind == "feedback"

    pause = asyncio.create_task(wl.measurement_pause_response())
    for _ in range(5):
        await asyncio.sleep(0)
    assert not pause.done(), tts_socket

    release_write.set()
    assert await asyncio.to_thread(write_returned.wait, 1.0)
    while tts._ring_end_monotonic is None:
        await asyncio.sleep(0)
    drain_at = tts.expected_drain_at()
    click.cancel()
    for _ in range(5):
        await asyncio.sleep(0)
    assert not click.done(), "accepted AUDIO tail must still hold feedback gate"
    assert not pause.done(), "PAUSE must still be draining feedback ownership"
    assert asyncio.get_running_loop().time() < drain_at

    with pytest.raises(asyncio.CancelledError):
        await click
    assert asyncio.get_running_loop().time() >= drain_at
    assert await asyncio.wait_for(pause, timeout=1.0) == {
        "result": "ok",
        "drained": True,
    }
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
        assert resp == {"result": "ok", "drained": True}
    finally:
        server.close()
        await server.wait_closed()
        await _close_window(wl)
        shutil.rmtree(sock_dir, ignore_errors=True)


async def test_old_coordinator_resumes_new_daemon_after_drain_timeout() -> None:
    """The legacy result stays ``ok`` so old cleanup logic still runs."""
    from jasper.voice.daemon_main import _start_control_socket

    wl = WakeLoop.for_tests()
    gate = _StuckGate()
    wl._output_gate = gate
    sock_dir = tempfile.mkdtemp(dir="/tmp", prefix="jts-uds-")
    socket_path = f"{sock_dir}/voice.sock"
    server = await _start_control_socket(wl, socket_path)
    try:
        response = await _voice_uds_command(socket_path, "MEASURE_PAUSE")
        assert response == {"result": "ok", "drained": False}

        # This is exactly the old coordinator's wire decision: it knows only
        # the scalar result, and must still renew/RESUME a pause that armed.
        if response.get("result") == "ok":
            resumed = await _voice_uds_command(socket_path, "MEASURE_RESUME")
            assert resumed == {"result": "ok"}

        assert not wl._measurement_active.is_set()
        assert not gate.admission_paused
    finally:
        server.close()
        await server.wait_closed()
        await _close_window(wl)
        shutil.rmtree(sock_dir, ignore_errors=True)
