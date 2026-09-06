# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""In-flight audio drain on MEASURE_PAUSE (issue #1898).

#1786 stopped proactive cues, timers, and research announcements from
*starting* once a room-correction measurement window is open. This is its
residual half: the pause checked only `State.SESSION`, never
`_output_gate.is_active`, so a cue or timer announcement that began a
moment BEFORE the PAUSE landed kept playing into the window's first
capture.

`MeasurementHold.pause()` now closes output admission atomically, arms the
window (so nothing new can start and mic frames stop immediately), and then
waits, bounded by `MEASUREMENT_INFLIGHT_DRAIN_SEC`, for the already-playing
episode to finish. On timeout it preserves the compatible `result=ok`, adds
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
import socket
import tempfile
import threading

import pytest

from jasper.measurement_window import (
    MEASUREMENT_LEASE_REFRESH_SEC,
    MEASUREMENT_LEASE_RETRY_SEC,
    VOICE_MEASURE_PAUSE_TIMEOUT_SEC,
    _voice_uds_command,
)
from jasper.tts_routing import FANIN_TTS_SOCKET, OUTPUTD_TTS_SOCKET
from jasper.voice.output_gate import AssistantOutputGate
from jasper.voice.measurement_hold import (
    MEASUREMENT_AUTOCLEAR_SEC,
    MEASUREMENT_INFLIGHT_DRAIN_SEC,
    MEASUREMENT_PAUSE_REPLY_MARGIN_SEC,
    MEASUREMENT_PAUSE_ROLLBACK_RESERVE_SEC,
    MEASUREMENT_PAUSE_SETUP_DRAIN_TIMEOUT_SEC,
    MEASUREMENT_PAUSE_TOTAL_TIMEOUT_SEC,
)
from jasper.voice_daemon import State, WakeLoop

from ._async_wait import wait_signalled


class _FakeMonotonic:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        assert seconds >= 0.0
        self.value += seconds


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


class _EndCountingGate(AssistantOutputGate):
    """Real gate that records exact episode-release ownership."""

    def __init__(self) -> None:
        super().__init__()
        self.end_calls = 0

    async def end(self, *args, **kwargs) -> None:
        self.end_calls += 1
        await super().end(*args, **kwargs)


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

    async def pause_content_meter_for_measurement(
        self, deadline_monotonic: float,
    ) -> None:
        return None

    async def resume_content_meter(self) -> None:
        return None


class _RefusingCues:
    """Cue manager that raises if asked to play — proves nothing played."""

    async def play(self, _slug: str) -> bool:
        raise AssertionError("cue must not play during a measurement window")


async def _close_window(wl: WakeLoop) -> None:
    """Cancel the auto-clear safety task so the test loop closes clean."""
    await wl.measurement_hold.resume()


async def _allow_test_measurement_meter(_deadline: float) -> None:
    """Non-transport fakes opt out of outputd's canonical-adapter contract."""

    return None


# --- the defect: an in-flight cue delays the window ------------------------


async def test_pause_waits_for_inflight_cue_then_returns() -> None:
    wl = WakeLoop.for_tests()
    episode = await wl._output_gate.begin_if_idle("admin")
    assert episode is not None

    pause = asyncio.create_task(wl.measurement_hold.pause())
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

        pause = asyncio.create_task(wl.measurement_hold.pause())
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

    pause = asyncio.create_task(wl.measurement_hold.pause())
    for _ in range(5):
        await asyncio.sleep(0)

    assert not pause.done()
    assert wl._measurement_active.is_set()
    # A crash mid-drain still self-heals: the safety timer is already armed.
    assert wl.measurement_hold._safety_task is not None
    assert not wl.measurement_hold._safety_task.done()
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

    pause = asyncio.create_task(wl.measurement_hold.pause())
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
        pause = asyncio.create_task(wl.measurement_hold.pause())
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
        result, drained = await wl.measurement_hold._pause_detailed()

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

    assert await wl.measurement_hold.pause() == "ok"
    assert wl._measurement_active.is_set()
    await _close_window(wl)


async def test_pause_setup_error_restores_output_admission_once() -> None:
    class _FailingPauseTts:
        def __init__(self) -> None:
            self.resume_calls = 0

        async def pause_content_meter_for_measurement(
            self, deadline_monotonic: float,
        ) -> None:
            raise RuntimeError("meter pause failed")

        async def resume_content_meter(self) -> None:
            self.resume_calls += 1

    wl = WakeLoop.for_tests()
    gate = _CountingGate()
    tts = _FailingPauseTts()
    wl._output_gate = gate
    wl._tts = tts

    with pytest.raises(RuntimeError, match="meter pause failed"):
        await wl.measurement_hold.pause()

    assert not wl._measurement_active.is_set()
    assert not gate.admission_paused
    assert gate.resume_calls == 1
    assert tts.resume_calls == 0


@pytest.mark.parametrize("error_type", [AssertionError, SystemExit])
async def test_unexpected_base_exception_after_opening_still_rolls_back(
    error_type: type[BaseException],
) -> None:
    class _UnexpectedMeter:
        async def pause_content_meter_for_measurement(
            self, deadline_monotonic: float,
        ) -> None:
            raise error_type("unexpected setup failure")

        async def resume_content_meter(self) -> None:
            raise AssertionError("a meter that never paused must not resume")

    wl = WakeLoop.for_tests()
    wl._tts = _UnexpectedMeter()

    with pytest.raises(error_type, match="unexpected setup failure"):
        await wl.measurement_hold.pause()

    assert not wl._measurement_active.is_set()
    assert not wl._output_gate.admission_paused
    assert wl.measurement_hold._safety_task is None


async def test_repeated_cancellation_waits_for_local_pause_rollback() -> None:
    setup_entered = asyncio.Event()
    rollback_entered = asyncio.Event()
    release_rollback = asyncio.Event()

    class _CancellableDrainGate(AssistantOutputGate):
        async def drain_paused(self, timeout: float) -> bool:
            del timeout
            setup_entered.set()
            await asyncio.Event().wait()
            return True

    class _HeldRollbackMeter:
        async def pause_content_meter_for_measurement(
            self, deadline_monotonic: float,
        ) -> None:
            return None

        async def resume_content_meter(self) -> None:
            rollback_entered.set()
            await wait_signalled(release_rollback, "release pause rollback")

    wl = WakeLoop.for_tests()
    gate = _CancellableDrainGate()
    episode = await gate.begin_if_idle("admin")
    assert episode is not None
    wl._output_gate = gate
    wl._tts = _HeldRollbackMeter()
    pause = asyncio.create_task(wl.measurement_hold.pause())
    await wait_signalled(setup_entered, "measurement setup", producer=pause)

    pause.cancel()
    await wait_signalled(rollback_entered, "measurement rollback", producer=pause)
    assert not wl._measurement_active.is_set()
    assert not wl._output_gate.admission_paused

    pause.cancel()
    await asyncio.sleep(0)
    pause.cancel()
    for _ in range(3):
        await asyncio.sleep(0)
    assert not pause.done()

    release_rollback.set()
    with pytest.raises(asyncio.CancelledError):
        await pause
    await gate.end(episode)


async def test_pause_arms_crash_recovery_before_external_setup_await() -> None:
    import jasper.voice.measurement_hold as measurement_hold_mod

    note_entered = asyncio.Event()
    release_note = asyncio.Event()

    class _FailingVolume:
        async def note_measurement_active(self, active: bool) -> None:
            if active:
                note_entered.set()
                await wait_signalled(release_note, "release volume guard setup")
                raise RuntimeError("volume guard failed")

    class _Meter:
        def __init__(self) -> None:
            self.resume_calls = 0

        async def pause_content_meter(self) -> None:
            return None

        async def resume_content_meter(self) -> None:
            self.resume_calls += 1

    wl = WakeLoop.for_tests()
    meter = _Meter()
    wl._volume_coordinator = _FailingVolume()
    wl._tts = meter

    pause = asyncio.create_task(wl.measurement_hold.pause())
    await wait_signalled(
        note_entered,
        "measurement volume setup",
        producer=pause,
    )
    assert wl._measurement_active.is_set()
    assert wl._output_gate.admission_paused
    safety = wl.measurement_hold._safety_task
    assert safety is not None and not safety.done()

    release_note.set()
    with pytest.raises(RuntimeError, match="volume guard failed"):
        await pause

    assert not wl._measurement_active.is_set()
    assert not wl._output_gate.admission_paused
    assert wl.measurement_hold._safety_task is None
    assert safety.done()
    assert meter.resume_calls == 0
    assert measurement_hold_mod.MEASUREMENT_AUTOCLEAR_SEC > 0


async def test_resume_reopens_admission_before_stuck_meter_recovers() -> None:
    resume_entered = asyncio.Event()
    release_resume = asyncio.Event()

    class _Meter:
        async def pause_content_meter_for_measurement(
            self, deadline_monotonic: float,
        ) -> None:
            return None

        async def resume_content_meter(self) -> None:
            resume_entered.set()
            await wait_signalled(release_resume, "release meter resume")

    wl = WakeLoop.for_tests()
    wl._tts = _Meter()
    assert await wl.measurement_hold.pause() == "ok"

    resume = asyncio.create_task(wl.measurement_hold.resume())
    await wait_signalled(
        resume_entered,
        "measurement meter resume",
        producer=resume,
    )
    assert not resume.done()
    assert not wl._measurement_active.is_set()
    assert not wl._output_gate.admission_paused

    release_resume.set()
    assert await resume == "ok"


async def test_resume_restores_after_safety_join_timeout(
    monkeypatch,
    caplog,
) -> None:
    import jasper.voice.measurement_hold as measurement_hold_mod

    monkeypatch.setattr(
        measurement_hold_mod,
        "MEASUREMENT_SAFETY_JOIN_TIMEOUT_SEC",
        0.01,
    )
    safety_started = asyncio.Event()
    release_safety = asyncio.Event()
    cancellation_seen = asyncio.Event()

    async def stubborn_safety() -> None:
        safety_started.set()
        try:
            await wait_signalled(release_safety, "release stubborn safety")
        except asyncio.CancelledError:
            cancellation_seen.set()
            await wait_signalled(
                release_safety,
                "release cancelled stubborn safety",
            )

    wl = WakeLoop.for_tests()
    await wl._output_gate.pause_admission()
    wl.measurement_hold._set_active_local(True, trigger="test")
    safety = asyncio.create_task(stubborn_safety())
    wl.measurement_hold._safety_task = safety
    await wait_signalled(
        safety_started,
        "stubborn measurement safety start",
        producer=safety,
    )

    assert await wl.measurement_hold.resume() == "ok"
    assert cancellation_seen.is_set()
    assert not wl._measurement_active.is_set()
    assert not wl._output_gate.admission_paused
    assert "event=measurement.safety_join_timeout" in caplog.text

    release_safety.set()
    await asyncio.wait_for(safety, timeout=1.0)


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

    pause = asyncio.create_task(wl.measurement_hold.pause())
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

    import jasper.audio_io as audio_io_mod
    from jasper.audio_io import TtsPlayout

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
    monkeypatch.setattr(audio_io_mod, "upsample_2x", lambda arr: arr)
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
            "Lock": staticmethod(asyncio.Lock),
            "create_task": staticmethod(asyncio.create_task),
            "current_task": staticmethod(asyncio.current_task),
            "shield": staticmethod(asyncio.shield),
            "to_thread": staticmethod(asyncio.to_thread),
            "wait": staticmethod(asyncio.wait),
            "sleep": staticmethod(fake_drain_sleep),
        },
    )
    monkeypatch.setattr(audio_io_mod, "asyncio", fake_asyncio)

    tts = TtsPlayout(
        socket_path=tts_socket,
        gain_db=-8.0,
        drain_tail_sec=1.0,
        # STATED, not inherited: the S16 mute-click bytes below are 10 bytes,
        # which an S32 wire cannot frame at all. `tts_wire_is_wide()` reads the
        # box's fanin.env — absent on a test runner, and an undeclared box is
        # WIDE since #3655 — so the width has to be declared here.
        wire_wide=False,
    )
    stream = _FailSecondWrite()
    tts._stream = stream  # type: ignore[assignment]
    tts.pause_content_meter_for_measurement = (  # type: ignore[method-assign]
        _allow_test_measurement_meter
    )
    wl = WakeLoop.for_tests(_earcon_wide=False)
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

    pause = asyncio.create_task(wl.measurement_hold.pause_response())
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
    from jasper.audio_io import TtsPlayout

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

    tts = TtsPlayout(
        socket_path=tts_socket,
        gain_db=-8.0,
        drain_tail_sec=1.0,
        # STATED, not inherited: the S16 mute-click bytes below are 10 bytes,
        # which an S32 wire cannot frame at all. `tts_wire_is_wide()` reads the
        # box's fanin.env — absent on a test runner, and an undeclared box is
        # WIDE since #3655 — so the width has to be declared here.
        wire_wide=False,
    )
    tts._stream = _BlockingWrite()  # type: ignore[assignment]
    tts.pause_content_meter_for_measurement = (  # type: ignore[method-assign]
        _allow_test_measurement_meter
    )
    wl = WakeLoop.for_tests(_earcon_wide=False)
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

    pause = asyncio.create_task(wl.measurement_hold.pause_response())
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


@pytest.mark.parametrize("tts_socket", [FANIN_TTS_SOCKET, OUTPUTD_TTS_SOCKET])
@pytest.mark.parametrize("path", ["admin", "proactive"])
async def test_cancelled_cue_tail_retains_output_episode(
    monkeypatch,
    tts_socket: str,
    path: str,
) -> None:
    """Accepted cue PCM keeps admin/proactive ownership under cancellation."""

    import jasper.audio_io as audio_io_mod
    from jasper.audio_io import TtsPlayout

    monkeypatch.setattr(audio_io_mod, "upsample_2x", lambda arr: arr)
    drain_started = asyncio.Event()
    release_drain = asyncio.Event()

    class _AcceptedStream:
        def set_gain_db(self, _db: float) -> None:
            return None

        def start_segment(self, **_kwargs) -> None:
            return None

        def write(self, _data: bytes) -> None:
            return None

    tts = TtsPlayout(
        socket_path=tts_socket,
        gain_db=-8.0,
        drain_tail_sec=0.0,
    )
    tts._stream = _AcceptedStream()  # type: ignore[assignment]
    tts.pause_content_meter_for_measurement = (  # type: ignore[method-assign]
        _allow_test_measurement_meter
    )

    async def controlled_drain() -> None:
        drain_started.set()
        await wait_signalled(release_drain, "release accepted cue tail")

    tts.wait_drained = controlled_drain  # type: ignore[method-assign]

    class _Cues:
        async def prerender_text(self, _text: str) -> bool:
            return True

        async def play(self, _slug: str) -> bool:
            await tts.write_segment(b"\x01\x00" * 5, segment_kind="cue")
            return True

        async def speak_text_guarded(self, _text: str, should_play) -> bool:
            if not should_play():
                return False
            await tts.write_segment(b"\x01\x00" * 5, segment_kind="cue")
            return True

    wl = WakeLoop.for_tests()
    wl._cfg.tts_outputd_socket = tts_socket
    wl._tts = tts
    wl._cues = _Cues()
    wl._camilla = None
    if path == "admin":
        playing = asyncio.create_task(wl._play_cue("cant_connect"))
        expected_kind = "admin"
    else:
        playing = asyncio.create_task(wl._play_dynamic_text("Timer finished"))
        expected_kind = "proactive"

    await wait_signalled(
        drain_started,
        f"{path} cue physical drain",
        producer=playing,
    )
    assert tts._ring_end_monotonic is not None
    playing.cancel()
    await asyncio.sleep(0)
    playing.cancel()
    for _ in range(5):
        await asyncio.sleep(0)
    assert not playing.done(), (tts_socket, path)
    assert wl._output_gate.active_kind == expected_kind

    pause = asyncio.create_task(wl.measurement_hold.pause_response())
    for _ in range(5):
        await asyncio.sleep(0)
    assert not pause.done(), (tts_socket, path)

    release_drain.set()
    with pytest.raises(asyncio.CancelledError):
        await playing
    assert await asyncio.wait_for(pause, timeout=1.0) == {
        "result": "ok",
        "drained": True,
    }
    await _close_window(wl)


@pytest.mark.parametrize(
    ("tts_socket", "duck_kind"),
    [
        (FANIN_TTS_SOCKET, "fanin"),
        (OUTPUTD_TTS_SOCKET, "cue"),
    ],
)
async def test_cancelled_proactive_tail_retains_concrete_duck_owner(
    tts_socket: str,
    duck_kind: str,
) -> None:
    """Both routed duck implementations restore only after physical drain."""

    from jasper.voice_daemon import FanInDucker

    tts = _TailHeldTts()
    ducked = asyncio.Event()
    restore_started = asyncio.Event()
    release_restore = asyncio.Event()
    restored = asyncio.Event()
    restore_calls = 0

    class _Cues:
        async def prerender_text(self, _text: str) -> bool:
            return True

        async def speak_text_guarded(self, _text: str, should_play) -> bool:
            return should_play()

    class _FanInDuck(FanInDucker):
        def __init__(self) -> None:
            self._ducked = False

        async def duck(self) -> None:
            self._ducked = True
            ducked.set()

        async def restore(self) -> None:
            nonlocal restore_calls
            restore_calls += 1
            restore_started.set()
            await wait_signalled(
                release_restore,
                "release fan-in proactive restore",
            )
            self._ducked = False
            restored.set()

    class _Fader:
        """The cue duck's half of the seam, now a claim on the volume owner.

        One door for both directions: the duck and its give-back are both
        absolute writes through the owner, discriminated by where they land.
        """

        def __init__(self) -> None:
            self.db = -20.0

        async def get_volume_db(self, *, best_effort: bool = False) -> float:
            return self.db

        async def set_volume_db(
            self, db: float, *, best_effort: bool = False,
        ) -> bool:
            nonlocal restore_calls
            if db < -20.0:
                self.db = db
                ducked.set()
                return True
            assert db == -20.0
            restore_calls += 1
            restore_started.set()
            await wait_signalled(
                release_restore,
                "release CueDuck proactive restore",
            )
            self.db = db
            restored.set()
            return True

    wl = WakeLoop.for_tests()
    gate = _EndCountingGate()
    wl._output_gate = gate
    wl._cfg.tts_outputd_socket = tts_socket
    wl._cfg.duck_db = -25.0
    wl._tts = tts
    wl._cues = _Cues()
    if duck_kind == "fanin":
        wl._ducker = _FanInDuck()
    else:
        from jasper.volume_owner import VolumeOwner

        wl._ducker = object()  # type: ignore[assignment]
        fader = _Fader()
        owner = VolumeOwner(
            set_fader_db=fader.set_volume_db,
            get_fader_db=fader.get_volume_db,
        )
        await owner.declare_household_level_db(-20.0)
        wl._volume_coordinator.volume_owner = owner  # type: ignore[attr-defined]

    playing = asyncio.create_task(wl._play_dynamic_text("Timer finished"))
    await wait_signalled(ducked, f"{duck_kind} proactive duck", producer=playing)
    await wait_signalled(
        tts.drain_started,
        f"{duck_kind} proactive physical drain",
        producer=playing,
    )

    playing.cancel()
    for _ in range(5):
        await asyncio.sleep(0)
    assert not playing.done(), (tts_socket, duck_kind)
    assert not restored.is_set(), "duck must cover accepted PCM tail"
    assert wl._output_gate.active_kind == "proactive"

    tts.release_drain.set()
    await wait_signalled(
        restore_started,
        f"{duck_kind} proactive restore",
        producer=playing,
    )
    assert not restored.is_set()
    assert wl._output_gate.active_kind == "proactive"

    playing.cancel()
    for _ in range(5):
        await asyncio.sleep(0)
    assert not playing.done(), "second cancel must not interrupt restore"
    assert wl._output_gate.active_kind == "proactive"

    release_restore.set()
    with pytest.raises(asyncio.CancelledError):
        await playing
    assert restored.is_set()
    assert not wl._output_gate.is_active
    assert restore_calls == 1
    assert gate.end_calls == 1


@pytest.mark.parametrize("path", ["admin", "proactive"])
@pytest.mark.parametrize("on_outcome", ["true", "false", "error"])
async def test_cancelled_fanin_duck_on_lands_then_cleanup_sends_off(
    monkeypatch,
    path: str,
    on_outcome: str,
) -> None:
    """Cancellation cannot orphan any possibly-delivered remote ON."""

    from jasper.voice_daemon import FanInDucker

    loop = asyncio.get_running_loop()
    on_started = asyncio.Event()
    off_started = asyncio.Event()
    release_on = threading.Event()
    release_off = threading.Event()
    commands: list[bytes] = []

    def send_command(payload: bytes) -> bool:
        commands.append(payload)
        if payload == b"PROGRAM_DUCK_ON\nCLOSE\n":
            loop.call_soon_threadsafe(on_started.set)
            if not release_on.wait(timeout=2.0):
                raise AssertionError("test did not release PROGRAM_DUCK_ON")
            if on_outcome == "false":
                return False
            if on_outcome == "error":
                raise RuntimeError("ambiguous PROGRAM_DUCK_ON failure")
        elif payload == b"PROGRAM_DUCK_OFF\nCLOSE\n":
            loop.call_soon_threadsafe(off_started.set)
            if not release_off.wait(timeout=2.0):
                raise AssertionError("test did not release PROGRAM_DUCK_OFF")
        else:
            raise AssertionError(f"unexpected fan-in command: {payload!r}")
        return True

    class _Tts:
        async def prepare_assistant_context(self, **_kwargs) -> None:
            return None

        async def wait_drained(self) -> None:
            return None

    class _Cues:
        calls = 0

        async def prerender_text(self, _text: str) -> bool:
            return True

        async def play(self, _slug: str) -> bool:
            self.calls += 1
            return True

        async def speak_text_guarded(self, _text: str, should_play) -> bool:
            if not should_play():
                return False
            self.calls += 1
            return True

    ducker = FanInDucker("/tmp/unused-fanin.sock", -25.0)
    monkeypatch.setattr(ducker, "_send_command", send_command)
    cues = _Cues()
    gate = _EndCountingGate()
    wl = WakeLoop.for_tests()
    wl._cfg.tts_outputd_socket = FANIN_TTS_SOCKET
    wl._output_gate = gate
    wl._ducker = ducker
    wl._tts = _Tts()
    wl._cues = cues
    if path == "admin":
        playing = asyncio.create_task(wl._play_cue("cant_connect"))
        expected_kind = "admin"
    else:
        playing = asyncio.create_task(wl._play_dynamic_text("Timer finished"))
        expected_kind = "proactive"

    try:
        await wait_signalled(
            on_started,
            f"{path} fan-in PROGRAM_DUCK_ON worker",
            producer=playing,
        )
        assert commands == [b"PROGRAM_DUCK_ON\nCLOSE\n"]
        assert wl._output_gate.active_kind == expected_kind

        playing.cancel()
        await asyncio.sleep(0)
        playing.cancel()
        for _ in range(5):
            await asyncio.sleep(0)
        assert not playing.done(), "ON outcome still owns cancellation"
        assert not ducker.is_ducked
        assert wl._output_gate.active_kind == expected_kind

        release_on.set()
        await wait_signalled(
            off_started,
            f"{path} fan-in PROGRAM_DUCK_OFF worker",
            producer=playing,
        )
        assert ducker.is_ducked
        assert commands == [
            b"PROGRAM_DUCK_ON\nCLOSE\n",
            b"PROGRAM_DUCK_OFF\nCLOSE\n",
        ]
        assert wl._output_gate.active_kind == expected_kind

        playing.cancel()
        for _ in range(5):
            await asyncio.sleep(0)
        assert not playing.done(), "cleanup must retain the gate through OFF"
        assert wl._output_gate.active_kind == expected_kind

        release_off.set()
        with pytest.raises(asyncio.CancelledError):
            await playing
        assert not ducker.is_ducked
        assert not wl._output_gate.is_active
        assert gate.end_calls == 1
        assert cues.calls == 0
        assert commands == [
            b"PROGRAM_DUCK_ON\nCLOSE\n",
            b"PROGRAM_DUCK_OFF\nCLOSE\n",
        ]
    finally:
        release_on.set()
        release_off.set()


@pytest.mark.parametrize("on_outcome", ["false", "error"])
async def test_fanin_ambiguous_on_owns_off_without_changing_original_semantics(
    monkeypatch,
    on_outcome: str,
) -> None:
    """False still returns and an unexpected error still propagates."""

    from jasper.voice_daemon import FanInDucker

    commands: list[bytes] = []
    worker_error = RuntimeError("ambiguous PROGRAM_DUCK_ON failure")

    def send_command(payload: bytes) -> bool:
        commands.append(payload)
        if payload == b"PROGRAM_DUCK_ON\nCLOSE\n":
            if on_outcome == "false":
                return False
            raise worker_error
        if payload == b"PROGRAM_DUCK_OFF\nCLOSE\n":
            return True
        raise AssertionError(f"unexpected fan-in command: {payload!r}")

    ducker = FanInDucker("/tmp/unused-fanin.sock", -25.0)
    monkeypatch.setattr(ducker, "_send_command", send_command)

    if on_outcome == "false":
        await ducker.duck()
    else:
        with pytest.raises(RuntimeError) as caught:
            await ducker.duck()
        assert caught.value is worker_error

    assert ducker.is_ducked
    await ducker.restore()
    assert not ducker.is_ducked
    assert commands == [
        b"PROGRAM_DUCK_ON\nCLOSE\n",
        b"PROGRAM_DUCK_OFF\nCLOSE\n",
    ]


@pytest.mark.parametrize("on_outcome", ["true", "false", "error"])
async def test_cancelled_begin_turn_owns_full_cleanup_through_fanin_off(
    monkeypatch,
    on_outcome: str,
) -> None:
    """A cancelled real begin resets once after every ambiguous ON result."""

    from jasper.voice_daemon import FanInDucker

    loop = asyncio.get_running_loop()
    on_started = asyncio.Event()
    off_started = asyncio.Event()
    release_on = threading.Event()
    release_off = threading.Event()
    commands: list[bytes] = []

    def send_command(payload: bytes) -> bool:
        commands.append(payload)
        if payload == b"PROGRAM_DUCK_ON\nCLOSE\n":
            loop.call_soon_threadsafe(on_started.set)
            if not release_on.wait(timeout=2.0):
                raise AssertionError("test did not release PROGRAM_DUCK_ON")
            if on_outcome == "false":
                return False
            if on_outcome == "error":
                raise RuntimeError("ambiguous PROGRAM_DUCK_ON failure")
            return True
        if payload == b"PROGRAM_DUCK_OFF\nCLOSE\n":
            loop.call_soon_threadsafe(off_started.set)
            if not release_off.wait(timeout=2.0):
                raise AssertionError("test did not release PROGRAM_DUCK_OFF")
            return True
        raise AssertionError(f"unexpected fan-in command: {payload!r}")

    class _Tts:
        def __init__(self) -> None:
            self.prepare_calls = 0
            self.pause_calls = 0
            self.resume_calls = 0

        async def prepare_assistant_context(self, **_kwargs) -> None:
            self.prepare_calls += 1

        async def pause_content_meter(self) -> None:
            self.pause_calls += 1

        async def resume_content_meter(self) -> None:
            self.resume_calls += 1

    class _ContentActivity:
        music_dbfs = None

        def __init__(self) -> None:
            self.refresh_calls = 0
            self.pause_calls = 0
            self.resume_calls = 0

        async def refresh_now(self) -> None:
            self.refresh_calls += 1

        def music_is_playing(self) -> bool:
            return False

        def pause(self) -> None:
            self.pause_calls += 1

        def resume(self) -> None:
            self.resume_calls += 1

    class _Volume:
        def __init__(self) -> None:
            self.session_calls: list[bool] = []

        def get_listening_level(self) -> int:
            return 50

        def note_voice_session(self, active: bool, **_kwargs) -> None:
            self.session_calls.append(active)

    class _UsageStore:
        def __init__(self) -> None:
            self.open_calls = 0
            self.close_calls = 0

        def open_session(self, **_kwargs) -> int:
            self.open_calls += 1
            return 1

        def close_session(self, *_args, **_kwargs) -> float:
            self.close_calls += 1
            return 0.0

    ducker = FanInDucker("/tmp/unused-fanin.sock", -25.0)
    monkeypatch.setattr(ducker, "_send_command", send_command)
    tts = _Tts()
    content = _ContentActivity()
    volume = _Volume()
    usage = _UsageStore()
    gate = _EndCountingGate()
    wl = WakeLoop.for_tests()
    wl._ducker = ducker
    wl._tts = tts
    wl._content_activity = content
    wl._volume_coordinator = volume
    wl._usage_store = usage
    wl._output_gate = gate
    cleanup_calls = 0
    real_cleanup = wl._cleanup_after_failed_begin

    async def counted_cleanup() -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        await real_cleanup()

    monkeypatch.setattr(wl, "_cleanup_after_failed_begin", counted_cleanup)
    beginning = asyncio.create_task(wl._begin_turn(pre_roll=False))

    try:
        await wait_signalled(
            on_started,
            "voice turn PROGRAM_DUCK_ON worker",
            producer=beginning,
        )
        assert gate.active_kind == "turn"
        assert volume.session_calls == [True]
        assert content.pause_calls == 1
        assert tts.pause_calls == 1

        beginning.cancel()
        for _ in range(5):
            await asyncio.sleep(0)
        assert not beginning.done()

        release_on.set()
        await wait_signalled(
            off_started,
            "voice turn PROGRAM_DUCK_OFF worker",
            producer=beginning,
        )
        assert ducker.is_ducked
        assert gate.active_kind == "turn"

        beginning.cancel()
        for _ in range(5):
            await asyncio.sleep(0)
        assert not beginning.done(), "repeated cancel must not interrupt OFF"
        assert gate.active_kind == "turn"

        release_off.set()
        with pytest.raises(asyncio.CancelledError):
            await beginning

        assert commands == [
            b"PROGRAM_DUCK_ON\nCLOSE\n",
            b"PROGRAM_DUCK_OFF\nCLOSE\n",
        ]
        assert not ducker.is_ducked
        assert cleanup_calls == 1
        assert gate.end_calls == 1
        assert not gate.is_active
        assert tts.prepare_calls == 1
        assert tts.pause_calls == 1
        assert tts.resume_calls == 1
        assert content.refresh_calls == 1
        assert content.pause_calls == 1
        assert content.resume_calls == 1
        assert volume.session_calls == [True, False]
        assert usage.open_calls == 0
        assert usage.close_calls == 0
        assert wl._turn is None
        assert wl._session_id is None
        assert wl._bg_tasks == set()
        assert wl._active_manual_source is None
        assert wl._acquiring is False
        assert wl._state is State.WAKE
        assert wl._turn_output_episode is None
    finally:
        release_on.set()
        release_off.set()


async def test_begin_turn_preserves_base_exception_after_owned_cleanup(
    monkeypatch,
) -> None:
    """The begin wrapper cleans and re-raises failures beyond Exception."""

    class _BeginAbort(BaseException):
        pass

    class _FailingContentActivity:
        music_dbfs = None

        def __init__(self) -> None:
            self.resume_calls = 0

        async def refresh_now(self) -> None:
            raise failure

        def pause(self) -> None:
            raise AssertionError("pause is after the injected failure")

        def resume(self) -> None:
            self.resume_calls += 1

    failure = _BeginAbort("begin aborted")
    content = _FailingContentActivity()
    gate = _EndCountingGate()
    wl = WakeLoop.for_tests()
    wl._content_activity = content
    wl._output_gate = gate
    cleanup_calls = 0
    real_cleanup = wl._cleanup_after_failed_begin

    async def counted_cleanup() -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        await real_cleanup()

    monkeypatch.setattr(wl, "_cleanup_after_failed_begin", counted_cleanup)

    with pytest.raises(_BeginAbort) as caught:
        await wl._begin_turn(pre_roll=False)

    assert caught.value is failure
    assert cleanup_calls == 1
    assert content.resume_calls == 1
    assert gate.end_calls == 1
    assert not gate.is_active
    assert wl._state is State.WAKE
    assert wl._turn_output_episode is None


@pytest.mark.parametrize("path", ["wake", "manual"])
async def test_cancelled_listening_feedback_prepare_owns_cleanup(
    monkeypatch,
    path: str,
) -> None:
    """Wake and manual prefixes retain their episode through repeated cancel."""

    prepare_started = asyncio.Event()
    restore_started = asyncio.Event()
    release_restore = asyncio.Event()

    class _HeldRestoreDucker:
        is_ducked = False

        def __init__(self) -> None:
            self.restore_calls = 0

        async def duck(self) -> None:
            raise AssertionError("turn inner must not start")

        async def restore(self) -> None:
            self.restore_calls += 1
            restore_started.set()
            await release_restore.wait()

    class _Tts:
        def __init__(self) -> None:
            self.resume_calls = 0

        async def resume_content_meter(self) -> None:
            self.resume_calls += 1

    class _ContentActivity:
        def __init__(self) -> None:
            self.resume_calls = 0

        def resume(self) -> None:
            self.resume_calls += 1

    class _Volume:
        def __init__(self) -> None:
            self.session_calls: list[bool] = []

        def note_voice_session(self, active: bool, **_kwargs) -> None:
            self.session_calls.append(active)

    async def held_prepare() -> None:
        prepare_started.set()
        await asyncio.Event().wait()

    ducker = _HeldRestoreDucker()
    tts = _Tts()
    content = _ContentActivity()
    volume = _Volume()
    gate = _EndCountingGate()
    wl = WakeLoop.for_tests()
    wl._ducker = ducker
    wl._tts = tts
    wl._content_activity = content
    wl._volume_coordinator = volume
    wl._output_gate = gate
    monkeypatch.setattr(wl, "_prepare_assistant_loudness_context", held_prepare)
    cleanup_calls = 0
    real_cleanup = wl._cleanup_after_failed_begin

    async def counted_cleanup() -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        await real_cleanup()

    monkeypatch.setattr(wl, "_cleanup_after_failed_begin", counted_cleanup)

    if path == "wake":
        async def win_arbitration(**_kwargs) -> str:
            return "WIN"

        wl._wake_late_cancelled = lambda *_args: False
        wl._peer_arbitrate = win_arbitration
        wl._acquiring = True
        beginning = asyncio.create_task(
            wl._arbitrate_acquire_drain(
                score=0.9,
                rms_dbfs=-30.0,
                spend_allowed=True,
                conn_paused=False,
                can_serve=True,
            )
        )
    else:
        beginning = asyncio.create_task(wl.manual_session_start())

    await wait_signalled(
        prepare_started,
        f"{path} listening-feedback loudness preparation",
        producer=beginning,
    )
    assert gate.active_kind == "turn"

    beginning.cancel("original prefix cancellation")
    await wait_signalled(
        restore_started,
        f"{path} failed-begin restore",
        producer=beginning,
    )
    assert gate.active_kind == "turn"

    beginning.cancel("repeat cleanup cancellation")
    for _ in range(5):
        await asyncio.sleep(0)
    assert not beginning.done()
    assert gate.active_kind == "turn"

    release_restore.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await beginning

    assert caught.value.args == ("original prefix cancellation",)
    assert beginning.cancelled()
    assert cleanup_calls == 1
    assert ducker.restore_calls == 1
    assert tts.resume_calls == 1
    assert content.resume_calls == 1
    assert volume.session_calls == [False]
    assert gate.end_calls == 1
    assert not gate.is_active
    assert wl._turn_output_episode is None
    assert wl._turn is None
    assert wl._session_id is None
    assert wl._bg_tasks == set()
    assert wl._state is State.WAKE
    assert wl._acquiring is False


@pytest.mark.parametrize(
    ("listening_feedback", "expected_events"),
    [
        (True, ["prepare", "chirp", "inner"]),
        (False, ["inner"]),
    ],
)
async def test_begin_turn_centralizes_feedback_prefix_without_reordering(
    monkeypatch,
    listening_feedback: bool,
    expected_events: list[str],
) -> None:
    """Wake/manual keep their prefix order; research-style begins add none."""

    events: list[str] = []
    gate = _EndCountingGate()
    wl = WakeLoop.for_tests()
    wl._output_gate = gate

    async def prepare() -> None:
        assert gate.active_kind == "turn"
        events.append("prepare")

    async def inner(**_kwargs) -> None:
        events.append("inner")

    def schedule(coro, *, name: str):
        assert name == "listening-chirp-on"
        coro.close()
        events.append("chirp")
        return None

    monkeypatch.setattr(wl, "_prepare_assistant_loudness_context", prepare)
    monkeypatch.setattr(wl, "_begin_turn_inner", inner)
    monkeypatch.setattr(wl, "_create_fire_and_forget_task", schedule)

    await wl._begin_turn(listening_feedback=listening_feedback)

    assert events == expected_events
    assert gate.is_active is listening_feedback
    if wl._turn_output_episode is not None:
        await gate.end_turn(wl._turn_output_episode)
        wl._turn_output_episode = None


@pytest.mark.parametrize(
    ("failure_kind", "failed_phase"),
    [
        ("restore_exception", "duck_restore"),
        ("meter_exception", "content_meter_resume"),
        ("usage_exception", "usage_session_close"),
        ("restore_base", None),
    ],
)
async def test_failed_begin_cleanup_runs_every_phase_after_phase_failure(
    caplog,
    failure_kind: str,
    failed_phase: str | None,
) -> None:
    """No ordinary or non-ordinary cleanup failure can wedge later phases."""

    class _CleanupAbort(BaseException):
        pass

    class _Turn:
        def __init__(self) -> None:
            self.release_calls = 0

        async def release(self) -> None:
            self.release_calls += 1

    class _Ducker:
        def __init__(self) -> None:
            self.restore_calls = 0

        async def restore(self) -> None:
            self.restore_calls += 1
            if failure_kind == "restore_exception":
                raise RuntimeError("duck restore failed")
            if failure_kind == "restore_base":
                raise _CleanupAbort("duck restore aborted")

    class _Volume:
        def __init__(self) -> None:
            self.session_calls: list[bool] = []

        def note_voice_session(self, active: bool, **_kwargs) -> None:
            self.session_calls.append(active)

    class _ContentActivity:
        def __init__(self) -> None:
            self.resume_calls = 0

        def resume(self) -> None:
            self.resume_calls += 1

    class _Tts:
        def __init__(self) -> None:
            self.resume_calls = 0

        async def resume_content_meter(self) -> None:
            self.resume_calls += 1
            if failure_kind == "meter_exception":
                raise RuntimeError("meter resume failed")

    class _UsageStore:
        def __init__(self) -> None:
            self.close_calls = 0

        def close_session(self, *_args) -> float:
            self.close_calls += 1
            if failure_kind == "usage_exception":
                raise RuntimeError("usage close failed")
            return 0.0

    begin_error = RuntimeError("original turn begin failure")

    async def failed_inner(**_kwargs) -> None:
        raise begin_error

    turn = _Turn()
    ducker = _Ducker()
    volume = _Volume()
    content = _ContentActivity()
    tts = _Tts()
    usage = _UsageStore()
    gate = _EndCountingGate()
    wl = WakeLoop.for_tests()
    wl._turn_output_episode = await gate.begin_turn()
    wl._turn = turn
    wl._session_id = 42
    wl._bg_tasks = {object()}  # type: ignore[assignment]
    wl._bg_end_scheduled = True
    wl._active_manual_source = "test_remote"
    wl._acquiring = True
    wl._state = State.SESSION
    wl._refractory_until = -1.0
    wl._ducker = ducker
    wl._volume_coordinator = volume
    wl._content_activity = content
    wl._tts = tts
    wl._usage_store = usage
    wl._output_gate = gate
    wl._begin_turn_inner = failed_inner

    with caplog.at_level(logging.WARNING, logger="jasper.voice_daemon"):
        with pytest.raises(RuntimeError) as caught:
            await wl._begin_turn(pre_roll=False)

    assert caught.value is begin_error
    assert turn.release_calls == 1
    assert ducker.restore_calls == 1
    assert volume.session_calls == [False]
    assert content.resume_calls == 1
    assert tts.resume_calls == 1
    assert usage.close_calls == 1
    assert wl._turn is None
    assert wl._session_id is None
    assert wl._bg_tasks == set()
    assert wl._bg_end_scheduled is False
    assert wl._active_manual_source is None
    assert wl._acquiring is False
    assert wl._state is State.WAKE
    assert wl._refractory_until > 0.0
    assert wl._turn_output_episode is None
    assert gate.end_calls == 1
    assert not gate.is_active
    if failed_phase is None:
        assert "event=turn.begin_cleanup_failed" in caplog.text
        assert "exc_type=_CleanupAbort" in caplog.text
    else:
        assert "event=turn.begin_cleanup_phase_failed" in caplog.text
        assert f"phase={failed_phase}" in caplog.text


async def test_duck_cleanup_logs_both_failures_and_releases_exactly_once(
    caplog,
) -> None:
    """Drain/restore Exceptions are both logged; exact release still wins."""

    class _FailingDrainTts:
        async def wait_drained(self) -> None:
            raise RuntimeError("physical drain exploded")

    restore_calls = 0

    async def failing_restore() -> None:
        nonlocal restore_calls
        restore_calls += 1
        raise ValueError("duck restore exploded")

    wl = WakeLoop.for_tests()
    gate = _EndCountingGate()
    wl._output_gate = gate
    wl._tts = _FailingDrainTts()
    episode = await gate.begin_if_idle("admin")
    assert episode is not None

    await wl._finish_ducked_output_episode_after_drain(
        episode,
        failing_restore,
        cleanup_label="contract cue",
    )

    assert restore_calls == 1
    assert gate.end_calls == 1
    assert not gate.is_active
    assert (
        "contract cue drain cleanup failed: physical drain exploded"
        in caplog.text
    )
    assert "contract cue restore failed: duck restore exploded" in caplog.text


@pytest.mark.parametrize("restore_fails", [False, True])
async def test_duck_cleanup_preserves_base_exception_precedence(
    restore_fails: bool,
) -> None:
    """Restore BaseException overrides drain; either follows exact release."""

    class _CleanupAbort(BaseException):
        pass

    drain_error = _CleanupAbort("drain aborted")
    restore_error = _CleanupAbort("restore aborted")

    class _FailingDrainTts:
        async def wait_drained(self) -> None:
            raise drain_error

    async def restore() -> None:
        if restore_fails:
            raise restore_error

    wl = WakeLoop.for_tests()
    gate = _EndCountingGate()
    wl._output_gate = gate
    wl._tts = _FailingDrainTts()
    episode = await gate.begin_if_idle("admin")
    assert episode is not None

    with pytest.raises(_CleanupAbort) as caught:
        await wl._finish_ducked_output_episode_after_drain(
            episode,
            restore,
            cleanup_label="contract cue",
        )

    expected_error = restore_error if restore_fails else drain_error
    assert caught.value is expected_error
    assert gate.end_calls == 1
    assert not gate.is_active


async def test_measurement_deadline_cleanup_preserves_cancelled_error() -> None:
    """The shared capture boundary does not convert deadline cancellation."""

    cancellation = asyncio.CancelledError("measurement cleanup cancelled")

    async def cancelled_step() -> None:
        raise cancellation

    wl = WakeLoop.for_tests()
    with pytest.raises(asyncio.CancelledError) as caught:
        await wl.measurement_hold._restore_step_before_deadline(
            cancelled_step(),
            deadline_monotonic=asyncio.get_running_loop().time() + 1.0,
            event="measurement.test_cleanup_failed",
            trigger="test",
        )

    assert caught.value is cancellation


@pytest.mark.parametrize("tts_socket", [FANIN_TTS_SOCKET, OUTPUTD_TTS_SOCKET])
async def test_cancelled_admin_cue_keeps_duck_until_physical_tail(
    monkeypatch,
    tmp_path,
    tts_socket: str,
) -> None:
    import wave

    import jasper.audio_io as audio_io_mod
    from jasper.audio_io import TtsPlayout
    from jasper.cues import AudioCueManager
    from jasper.cues.registry import find

    monkeypatch.setattr(audio_io_mod, "upsample_2x", lambda arr: arr)
    drain_started = asyncio.Event()
    release_drain = asyncio.Event()
    restore_started = asyncio.Event()
    release_restore = asyncio.Event()
    restored = asyncio.Event()
    restore_calls = 0

    class _AcceptedStream:
        def set_gain_db(self, _db: float) -> None:
            return None

        def start_segment(self, **_kwargs) -> None:
            return None

        def write(self, _data: bytes) -> None:
            return None

    class _Ducker:
        async def duck(self) -> None:
            return None

        async def restore(self) -> None:
            nonlocal restore_calls
            restore_calls += 1
            restore_started.set()
            await wait_signalled(
                release_restore,
                "release admin cue restore",
            )
            restored.set()

    tts = TtsPlayout(
        socket_path=tts_socket,
        gain_db=-8.0,
        drain_tail_sec=0.0,
    )
    tts._stream = _AcceptedStream()  # type: ignore[assignment]

    async def controlled_drain() -> None:
        drain_started.set()
        await wait_signalled(release_drain, "release ducked cue tail")

    tts.wait_drained = controlled_drain  # type: ignore[method-assign]
    cues = AudioCueManager(
        sounds_dir=str(tmp_path),
        hostname="jts.local",
        voice="Aoede",
        tts_playout=tts,
    )
    cue = find("cant_connect")
    assert cue is not None
    cue_path = cues.expected_path(cue)
    with wave.open(cue_path, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(b"\x01\x00" * 5)

    wl = WakeLoop.for_tests()
    gate = _EndCountingGate()
    wl._output_gate = gate
    wl._cfg.tts_outputd_socket = tts_socket
    wl._tts = tts
    wl._cues = cues
    wl._ducker = _Ducker()
    playing = asyncio.create_task(wl._play_cue("cant_connect"))
    await wait_signalled(
        drain_started,
        "admin cue manager physical drain",
        producer=playing,
    )

    playing.cancel()
    for _ in range(5):
        await asyncio.sleep(0)
    assert not playing.done(), tts_socket
    assert not restored.is_set(), "duck restore must follow the physical tail"
    assert wl._output_gate.active_kind == "admin"

    release_drain.set()
    await wait_signalled(
        restore_started,
        "admin cue restore",
        producer=playing,
    )
    assert not restored.is_set()
    assert wl._output_gate.active_kind == "admin"

    playing.cancel()
    for _ in range(5):
        await asyncio.sleep(0)
    assert not playing.done(), "second cancel must not interrupt restore"
    assert wl._output_gate.active_kind == "admin"

    release_restore.set()
    with pytest.raises(asyncio.CancelledError):
        await playing
    assert restored.is_set()
    assert not wl._output_gate.is_active
    assert restore_calls == 1
    assert gate.end_calls == 1


async def test_lease_refresh_into_an_open_window_never_waits() -> None:
    """The coordinator re-sends MEASURE_PAUSE every
    MEASUREMENT_LEASE_REFRESH_SEC to renew the daemon's crash-recovery
    lease. The first capture began long ago and
    #1786 blocks new output, so a renewal has nothing to drain and must
    stay latency-free even if something is somehow playing."""
    wl = WakeLoop.for_tests()
    assert await wl.measurement_hold.pause() == "ok"

    gate = _StuckGate()
    wl._output_gate = gate
    assert await wl.measurement_hold.pause() == "ok"

    assert gate.waits == []
    await _close_window(wl)


async def test_lease_refresh_joins_stale_auto_clear_before_return(
    monkeypatch,
) -> None:
    """An expiring old lease cannot reopen admission behind its renewal."""
    import jasper.voice.measurement_hold as measurement_hold_mod

    old_sleeping = asyncio.Event()
    release_old = asyncio.Event()
    old_released = asyncio.Event()
    new_sleeping = asyncio.Event()
    keep_new_armed = asyncio.Event()
    calls = 0

    async def controlled_safety_sleep(_seconds: float) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            old_sleeping.set()
            await wait_signalled(release_old, "release old measurement safety")
            old_released.set()
            return
        new_sleeping.set()
        await wait_signalled(keep_new_armed, "renewed safety cancellation")

    monkeypatch.setattr(
        measurement_hold_mod,
        "_measurement_safety_sleep",
        controlled_safety_sleep,
    )
    wl = WakeLoop.for_tests()
    assert await wl.measurement_hold.pause() == "ok"
    await wait_signalled(old_sleeping, "old measurement safety sleep")
    old_task = wl.measurement_hold._safety_task
    assert old_task is not None

    await wl.measurement_hold._transition_lock.acquire()
    try:
        renewal = asyncio.create_task(wl.measurement_hold.pause())
        await asyncio.sleep(0)
        release_old.set()
        await wait_signalled(
            old_released,
            "old measurement safety expiry",
            producer=old_task,
        )
        await asyncio.sleep(0)
    finally:
        wl.measurement_hold._transition_lock.release()

    assert await renewal == "ok"
    await wait_signalled(new_sleeping, "renewed measurement safety sleep")
    assert old_task.done()
    assert wl.measurement_hold._safety_task is not old_task
    assert wl._measurement_active.is_set()
    assert wl._output_gate.admission_paused
    await _close_window(wl)


async def test_renewal_timeout_releases_lock_for_auto_clear(monkeypatch) -> None:
    """An expiring setup cannot starve the generation-bound backstop."""

    import jasper.voice.measurement_hold as measurement_hold_mod

    clock = _FakeMonotonic()
    monkeypatch.setattr(measurement_hold_mod, "_measurement_monotonic", clock)
    safety_started = asyncio.Event()
    expire_safety = asyncio.Event()

    async def controlled_safety_sleep(_seconds: float) -> None:
        safety_started.set()
        await wait_signalled(expire_safety, "expire renewed measurement lease")

    monkeypatch.setattr(
        measurement_hold_mod,
        "_measurement_safety_sleep",
        controlled_safety_sleep,
    )

    class _ExpiringVolume:
        async def note_measurement_active(self, active: bool) -> None:
            if active:
                clock.advance(MEASUREMENT_PAUSE_SETUP_DRAIN_TIMEOUT_SEC)
                raise TimeoutError("volume setup exhausted aggregate budget")

    wl = WakeLoop.for_tests()
    await wl._output_gate.pause_admission()
    wl.measurement_hold._set_active_local(True, trigger="test")
    wl._volume_coordinator = _ExpiringVolume()

    with pytest.raises(TimeoutError, match="aggregate deadline"):
        await wl.measurement_hold.pause()
    await wait_signalled(
        safety_started,
        "renewed safety task",
        producer=wl.measurement_hold._safety_task,
    )
    expire_safety.set()
    safety = wl.measurement_hold._safety_task
    assert safety is not None
    await safety

    assert not wl._measurement_active.is_set()
    assert not wl._output_gate.admission_paused


async def test_active_session_still_refuses_without_draining() -> None:
    wl = WakeLoop.for_tests()
    wl._state = State.SESSION
    gate = _StuckGate()
    wl._output_gate = gate

    assert await wl.measurement_hold.pause() == "BUSY"
    assert not wl._measurement_active.is_set()
    assert gate.waits == []


# --- protocol compatibility, both deploy directions ------------------------


def test_aggregate_pause_budget_fits_under_coordinator_read_timeout() -> None:
    """OLD coordinator + NEW daemon. install.sh restarts jasper-voice and
    jasper-web at different points of a deploy, so a coordinator pinned to
    the timeout it shipped with can be awaiting a daemon that now holds
    the reply through a drain. A coordinator that gives up believes voice
    was never paused, skips MEASURE_RESUME, and leaves the speaker gated
    until the daemon's auto-clear — so the bound must fit under the
    timeout with room for connect/write/scheduling on a loaded Pi."""
    assert (
        MEASUREMENT_PAUSE_TOTAL_TIMEOUT_SEC
        + MEASUREMENT_PAUSE_REPLY_MARGIN_SEC
        == VOICE_MEASURE_PAUSE_TIMEOUT_SEC
    )
    assert (
        MEASUREMENT_PAUSE_SETUP_DRAIN_TIMEOUT_SEC
        + MEASUREMENT_PAUSE_ROLLBACK_RESERVE_SEC
        == MEASUREMENT_PAUSE_TOTAL_TIMEOUT_SEC
    )
    assert MEASUREMENT_INFLIGHT_DRAIN_SEC <= (
        MEASUREMENT_PAUSE_SETUP_DRAIN_TIMEOUT_SEC
    )
    # Fan-in can hold 1.2 s ahead, plus one 250 ms chunk and the 85 ms
    # physical-tail allowance. Keep Pi scheduler headroom above that 1.535 s.
    assert MEASUREMENT_INFLIGHT_DRAIN_SEC >= 1.7


async def test_uds_slow_setup_reduces_drain_to_aggregate_remaining(
    monkeypatch,
) -> None:
    """The real wire shares one budget across volume, meter, and drain."""

    import jasper.voice.measurement_hold as measurement_hold_mod
    from jasper.voice.daemon_main import _start_control_socket

    clock = _FakeMonotonic()
    monkeypatch.setattr(measurement_hold_mod, "_measurement_monotonic", clock)

    class _SlowVolume:
        async def note_measurement_active(self, active: bool) -> None:
            if active:
                clock.advance(0.30)

    class _SlowMeter:
        async def pause_content_meter_for_measurement(
            self, deadline_monotonic: float,
        ) -> None:
            clock.advance(0.25)

        async def resume_content_meter(self) -> None:
            return None

    class _BudgetGate(_StuckGate):
        async def drain_paused(self, timeout: float) -> bool:
            self.waits.append(timeout)
            clock.advance(timeout)
            return False

    wl = WakeLoop.for_tests()
    gate = _BudgetGate()
    wl._output_gate = gate
    wl._volume_coordinator = _SlowVolume()
    wl._tts = _SlowMeter()
    started = clock()
    sock_dir = tempfile.mkdtemp(dir="/tmp", prefix="jts-uds-shared-budget-")
    socket_path = f"{sock_dir}/voice.sock"
    server = await _start_control_socket(wl, socket_path)
    try:
        assert await _voice_uds_command(
            socket_path,
            "MEASURE_PAUSE",
            timeout=VOICE_MEASURE_PAUSE_TIMEOUT_SEC,
        ) == {
            "result": "ok",
            "drained": False,
        }
        assert gate.waits == [pytest.approx(1.70)]
        assert clock() - started == pytest.approx(
            MEASUREMENT_PAUSE_SETUP_DRAIN_TIMEOUT_SEC
        )
    finally:
        server.close()
        await server.wait_closed()
        await _close_window(wl)
        shutil.rmtree(sock_dir, ignore_errors=True)


async def test_uds_setup_expiry_rolls_back_inside_declared_total(
    monkeypatch,
) -> None:
    """The real wire returns non-ok after local rollback, below old 3 s."""

    import jasper.voice.measurement_hold as measurement_hold_mod
    from jasper.voice.daemon_main import _start_control_socket

    clock = _FakeMonotonic()
    monkeypatch.setattr(measurement_hold_mod, "_measurement_monotonic", clock)

    class _SlowVolume:
        async def note_measurement_active(self, active: bool) -> None:
            clock.advance(0.40 if active else 0.10)

    class _ExpiringMeter:
        resume_calls = 0

        async def pause_content_meter_for_measurement(
            self,
            deadline_monotonic: float,
        ) -> None:
            clock.advance(deadline_monotonic - clock())
            raise TimeoutError("declared meter setup maximum reached")

        async def pause_content_meter(self) -> None:
            raise AssertionError("measurement-specific path must be selected")

        async def resume_content_meter(self) -> None:
            self.resume_calls += 1

    wl = WakeLoop.for_tests()
    meter = _ExpiringMeter()
    wl._volume_coordinator = _SlowVolume()
    wl._tts = meter
    started = clock()
    sock_dir = tempfile.mkdtemp(dir="/tmp", prefix="jts-uds-budget-")
    socket_path = f"{sock_dir}/voice.sock"
    server = await _start_control_socket(wl, socket_path)
    try:
        response = await _voice_uds_command(
            socket_path,
            "MEASURE_PAUSE",
            timeout=VOICE_MEASURE_PAUSE_TIMEOUT_SEC,
        )
        assert response == {"result": "ERROR"}
        assert clock() - started == pytest.approx(
            MEASUREMENT_PAUSE_SETUP_DRAIN_TIMEOUT_SEC + 0.10
        )
        assert clock() - started < MEASUREMENT_PAUSE_TOTAL_TIMEOUT_SEC
        assert MEASUREMENT_PAUSE_TOTAL_TIMEOUT_SEC < (
            VOICE_MEASURE_PAUSE_TIMEOUT_SEC
        )
        assert not wl._measurement_active.is_set()
        assert not wl._output_gate.admission_paused
        assert meter.resume_calls == 0
    finally:
        server.close()
        await server.wait_closed()
        await _close_window(wl)
        shutil.rmtree(sock_dir, ignore_errors=True)


async def test_uds_poisoned_meter_fails_closed_then_reconnects_on_next_access(
    monkeypatch,
) -> None:
    """MEASURE_PAUSE never reconnects; a later ordinary control does once."""

    import jasper.audio_io as audio_io_mod
    from jasper.audio_io import TtsPlayout
    from jasper.voice.daemon_main import _start_control_socket

    parent, child = socket.socketpair()
    poisoned = audio_io_mod._OutputdStreamAdapter(parent)
    poisoned.close()
    child.close()
    tts = TtsPlayout(socket_path="/tmp/outputd-test.sock")
    tts._stream = poisoned  # type: ignore[assignment]
    connect_calls = 0

    class _Replacement:
        def __init__(self) -> None:
            self.meter_pauses = 0

        def pause_content_meter(self) -> None:
            self.meter_pauses += 1

    replacement = _Replacement()

    async def fake_connect():
        nonlocal connect_calls
        connect_calls += 1
        return replacement

    monkeypatch.setattr(tts, "_connect_stream_adapter", fake_connect)
    wl = WakeLoop.for_tests()
    wl._tts = tts
    sock_dir = tempfile.mkdtemp(dir="/tmp", prefix="jts-uds-poisoned-")
    socket_path = f"{sock_dir}/voice.sock"
    server = await _start_control_socket(wl, socket_path)
    try:
        response = await _voice_uds_command(
            socket_path,
            "MEASURE_PAUSE",
            timeout=VOICE_MEASURE_PAUSE_TIMEOUT_SEC,
        )
        assert response == {"result": "ERROR"}
        assert connect_calls == 0
        assert tts._stream is poisoned
        assert not wl._measurement_active.is_set()
        assert not wl._output_gate.admission_paused

        await tts.pause_content_meter()
        assert connect_calls == 1
        assert tts._stream is replacement
        assert replacement.meter_pauses == 1
    finally:
        server.close()
        await server.wait_closed()
        await _close_window(wl)
        shutil.rmtree(sock_dir, ignore_errors=True)


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
    `_refresh_voice_lease` in measurement_window.py. Budget only the retry
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
