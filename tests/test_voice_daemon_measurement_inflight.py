# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""MEASURE_PAUSE's in-flight drain, and the output-episode ownership it trusts.

`MeasurementHold.pause_response()` closes output admission, arms the window
and its auto-clear backstop, then waits — bounded by
`MEASUREMENT_INFLIGHT_DRAIN_SEC` and the aggregate setup budget — for the
episode that was already playing (issue #1898). A drain that runs out keeps
`result=ok` and adds `drained=false`: an OLD coordinator, which branches on
`result` alone and awaits the reply with `VOICE_MEASURE_PAUSE_TIMEOUT_SEC`,
can meet a NEW daemon mid-deploy and must still renew and send
MEASURE_RESUME.

The drain is only as good as episode ownership, so the cancellation pins for
cues, mute clicks and turn begins live here too: an episode is released only
once its accepted PCM has physically drained and its duck is restored.

The #1786 refusals live in tests/test_voice_daemon_measurement_gate.py; the
gate's own drain primitive in tests/test_voice_output_gate.py.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import threading
import wave
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import jasper.tts_playout as tts_mod
import jasper.voice.measurement_hold as measurement_hold_mod
from jasper.cues import AudioCueManager
from jasper.cues.registry import find
from jasper.measurement_window import (
    MEASUREMENT_LEASE_REFRESH_SEC,
    MEASUREMENT_LEASE_RETRY_SEC,
    VOICE_MEASURE_PAUSE_TIMEOUT_SEC,
    _voice_uds_command,
)
from jasper.tts_playout import TtsPlayout
from jasper.voice.control_socket import serve
from jasper.voice.measurement_hold import (
    MEASUREMENT_AUTOCLEAR_SEC,
    MEASUREMENT_INFLIGHT_DRAIN_SEC,
    MEASUREMENT_PAUSE_REPLY_MARGIN_SEC,
    MEASUREMENT_PAUSE_ROLLBACK_RESERVE_SEC,
    MEASUREMENT_PAUSE_SETUP_DRAIN_TIMEOUT_SEC,
    MEASUREMENT_PAUSE_TOTAL_TIMEOUT_SEC,
    MeasurementHold,
)
from jasper.voice.output_gate import AssistantOutputGate
from jasper.voice.turn_lifecycle import State
from jasper.voice_daemon import FanInDucker, WakeLoop

from ._async_wait import settle, wait_signalled
from ._cue_spy import SpyCues
from ._live_turn_fake import FakeLiveTurn
from ._log_events import event_fields, event_records
from ._playout import FakeOutputdStream, FakeTts, playout_over_fake_stream
from ._socket_paths import short_socket_path_fixture as _short_sock_path_fixture
from ._wake_loop import wake_loop_for_tests
from .fake_clock_fixtures import FakeClock
from .usage_store_fixtures import FakeUsageStore

_IMPORTED_FIXTURES = (_short_sock_path_fixture,)

# Five S16 frames: enough accepted PCM to leave a physical tail.
_CUE_PCM = b"\x01\x00" * 5


class _Abort(BaseException):
    """A failure outside `Exception`, which no ordinary handler may absorb."""


class _Hold:
    """An awaitable step that announces it started, then waits for release."""

    def __init__(self, what: str) -> None:
        self.what = what
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, *_args: object, **_kwargs: object) -> None:
        self.started.set()
        await wait_signalled(self.release, f"release {self.what}")


class _SpyGate(AssistantOutputGate):
    """The real gate, observable: drain entry, admission resumes, releases.

    `hold_drain` parks the drain until cancelled, so a cancellation pin never
    races the real drain's own timeout.
    """

    def __init__(self, *, hold_drain: bool = False) -> None:
        super().__init__()
        self._hold_drain = hold_drain
        self.drain_entered = asyncio.Event()
        self.resume_calls = 0
        self.end_calls = 0

    async def drain_paused(self, timeout: float) -> bool:
        self.drain_entered.set()
        if self._hold_drain:
            await asyncio.Event().wait()
        return await super().drain_paused(timeout)

    async def resume_admission(self) -> bool:
        self.resume_calls += 1
        return await super().resume_admission()

    async def end(self, *args, **kwargs) -> None:
        self.end_calls += 1
        await super().end(*args, **kwargs)


class _StuckGate:
    """An episode that never ends, with no real sleep: the drain reports its
    timeout at once, advancing `clock` (when given) by the bound it was given."""

    is_active = True
    active_kind = "proactive"

    def __init__(self, clock: FakeClock | None = None) -> None:
        self._clock = clock
        self.admission_paused = False
        self.waits: list[float] = []

    async def pause_admission(self) -> bool:
        changed = not self.admission_paused
        self.admission_paused = True
        return changed

    async def drain_paused(self, timeout: float) -> bool:
        self.waits.append(timeout)
        if self._clock is not None:
            self._clock.now += timeout
        return False

    async def resume_admission(self) -> bool:
        changed = self.admission_paused
        self.admission_paused = False
        return changed


class _Content:
    """Content-activity double counting what a turn's begin and cleanup call."""

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
    """Records voice sessions; runs `on_measurement` for each guard change."""

    def __init__(self, on_measurement=None) -> None:
        self._on_measurement = on_measurement
        self.session_calls: list[bool] = []

    def get_listening_level(self) -> int:
        return 50

    def note_voice_session(self, active: bool, **_kwargs) -> None:
        self.session_calls.append(active)

    async def note_measurement_active(self, active: bool) -> None:
        if self._on_measurement is not None:
            await self._on_measurement(active)


class _Ducker:
    """Duck double whose restore can be held open, or made to fail."""

    is_ducked = False

    def __init__(
        self,
        *,
        hold_restore: bool = False,
        restore_error: BaseException | None = None,
    ) -> None:
        self._restore_error = restore_error
        self.restore_hold = _Hold("duck restore")
        if not hold_restore:
            self.restore_hold.release.set()
        self.duck_calls = 0
        self.restore_calls = 0
        self.restored = False

    async def duck(self) -> None:
        self.duck_calls += 1

    async def restore(self) -> None:
        self.restore_calls += 1
        await self.restore_hold()
        if self._restore_error is not None:
            raise self._restore_error
        self.restored = True


class _SpeakingCues(SpyCues):
    """Spy whose dynamic text reaches the playout as cue PCM."""

    def __init__(self, tts: TtsPlayout) -> None:
        super().__init__()
        self._tts = tts

    async def speak_text(self, text: str, should_play=None) -> bool:
        if not await super().speak_text(text, should_play):
            return False
        await self._tts.write_segment(_CUE_PCM, segment_kind="cue")
        return True


class _RemoteDuck:
    """Fan-in's PROGRAM_DUCK verb on a worker thread, as the socket write runs.

    Each command blocks until its `release` is set, so a test can cancel the
    caller mid-command; `on_outcome` is what the ON command then reports.
    """

    def __init__(self, on_outcome: str, *, hold: bool = True) -> None:
        self._loop = asyncio.get_running_loop()
        self._on_outcome = on_outcome
        self.error = RuntimeError("ambiguous PROGRAM_DUCK_ON failure")
        self.commands: list[bool] = []
        self.started = {True: asyncio.Event(), False: asyncio.Event()}
        self.release = {True: threading.Event(), False: threading.Event()}
        if not hold:
            self.release_all()

    def release_all(self) -> None:
        for event in self.release.values():
            event.set()

    def _command(self, on: bool) -> bool:
        self.commands.append(on)
        self._loop.call_soon_threadsafe(self.started[on].set)
        if not self.release[on].wait(timeout=2.0):
            raise AssertionError(f"test did not release PROGRAM_DUCK on={on}")
        if on and self._on_outcome == "false":
            return False
        if on and self._on_outcome == "error":
            raise self.error
        return True

    async def program_duck(self, on: bool) -> bool:
        return await asyncio.to_thread(self._command, on)


def _playout(**kwargs) -> TtsPlayout:
    """A real playout over a capturing stream. That stream is no outputd
    transport, so the measurement meter pause (which demands one) is skipped."""
    tts, _ = playout_over_fake_stream(**kwargs)
    tts.pause_content_meter_for_measurement = AsyncMock()  # type: ignore[method-assign]
    return tts


def _mute_click_rig(on_write) -> tuple[WakeLoop, TtsPlayout]:
    """A real playout whose mute click is `_CUE_PCM`. The width is STATED:
    those 10 bytes cannot frame on an S32 wire, and an undeclared box is WIDE
    (#3655)."""
    tts = _playout(drain_tail_sec=1.0, on_write=on_write, wire_wide=False)
    wl = wake_loop_for_tests(tts=tts)
    wl._assistant_output._earcon_wide = False
    wl._assistant_output._mute_click_on_pcm = _CUE_PCM
    return wl, tts


@contextlib.asynccontextmanager
async def _serving(wl: WakeLoop, socket_path: str) -> AsyncIterator[None]:
    """The real control socket; the window is closed again on the way out."""
    server = await serve(wl, socket_path)
    try:
        yield
    finally:
        server.close()
        await server.wait_closed()
        await wl.measurement_hold.resume()


def _count_failed_begin_cleanups(wl: WakeLoop, monkeypatch) -> list[None]:
    calls: list[None] = []
    real = wl._turns.cleanup_after_failed_begin

    async def counted() -> None:
        calls.append(None)
        await real()

    monkeypatch.setattr(wl._turns, "cleanup_after_failed_begin", counted)
    return calls


def _assert_turn_released(wl: WakeLoop, gate: _SpyGate) -> None:
    """A failed or cancelled begin left nothing owned, released exactly once."""
    assert gate.end_calls == 1
    assert not gate.is_active
    assert wl._turns.output_episode is None
    assert wl._turns.turn is None
    assert wl._turns.session_id is None
    assert wl._turns.bg_tasks == set()
    assert wl._turns.state is State.WAKE
    assert wl._push_to_talk.active_source is None
    assert wl._acquiring is False


async def _cancel_through_remote_duck(
    owner: asyncio.Task,
    remote: _RemoteDuck,
    ducker: FanInDucker,
    gate: _SpyGate,
    kind: str,
) -> None:
    """Cancel `owner` while its PROGRAM_DUCK_ON is in flight, and again while
    the OFF it then owes is: it keeps its episode until that OFF has landed."""
    assert remote.commands == [True]
    assert gate.active_kind == kind
    owner.cancel()
    await asyncio.sleep(0)
    owner.cancel()
    await settle(5)
    assert not owner.done(), "ON outcome still owns cancellation"
    assert not ducker.is_ducked
    assert gate.active_kind == kind

    remote.release[True].set()
    await wait_signalled(remote.started[False], f"{kind} PROGRAM_DUCK_OFF", producer=owner)
    assert ducker.is_ducked
    assert gate.active_kind == kind

    owner.cancel()
    await settle(5)
    assert not owner.done(), "cleanup must retain the gate through OFF"
    assert gate.active_kind == kind

    remote.release[False].set()
    with pytest.raises(asyncio.CancelledError):
        await owner
    assert remote.commands == [True, False]
    assert not ducker.is_ducked
    assert not gate.is_active
    assert gate.end_calls == 1


# --- the drain -------------------------------------------------------------


@pytest.mark.parametrize("kind", ["admin", "proactive", "turn"])
async def test_pause_reply_waits_inside_the_drain_for_the_inflight_episode(
    caplog, short_sock_path: str, kind: str,
) -> None:
    """Ordering is load-bearing: admission, the measurement flag (read by
    every #1786 entry point and the mic-frame gate) and the crash backstop
    are all armed BEFORE the drain, so nothing new can start and a crash
    mid-drain still self-heals. The drain waits the episode out and never
    revokes it — a wake-blocking cue already speaking finishes — and an OLD
    coordinator's read timeout outlasts the held reply."""
    caplog.set_level(logging.INFO, logger="jasper.voice_daemon")
    gate = _SpyGate()
    cues = SpyCues()
    wl = wake_loop_for_tests(output_gate=gate, cues=cues)
    episode = await (
        gate.begin_turn() if kind == "turn" else gate.begin_if_idle(kind)
    )
    assert episode is not None
    async with _serving(wl, short_sock_path):
        reply = asyncio.create_task(_voice_uds_command(
            short_sock_path, "MEASURE_PAUSE", timeout=VOICE_MEASURE_PAUSE_TIMEOUT_SEC,
        ))
        await wait_signalled(gate.drain_entered, "MEASURE_PAUSE drain", producer=reply)
        assert wl._measurement_active.is_set()
        safety = wl.measurement_hold._safety_task
        assert safety is not None and not safety.done()
        assert await wl.play_cue("cant_connect") == "measurement_active"
        assert gate.is_current(episode)
        assert not reply.done()

        await gate.end(episode)
        assert await reply == {"result": "ok", "drained": True}
    assert event_fields(caplog, "measurement.inflight_drained")["active_kind"] == kind
    assert cues.played == []


@pytest.mark.parametrize(
    ("guard_sec", "meter_sec", "drain_bound"),
    [(0.0, 0.0, MEASUREMENT_INFLIGHT_DRAIN_SEC), (0.30, 0.25, 1.70)],
)
async def test_drain_timeout_stays_ok_inside_one_setup_budget(
    caplog,
    monkeypatch,
    short_sock_path: str,
    guard_sec: float,
    meter_sec: float,
    drain_bound: float,
) -> None:
    """Volume guard, meter and drain share one budget, and a drain that runs
    out is additive `drained=false` evidence: an OLD coordinator sees only
    `result=ok`, so it still renews and sends MEASURE_RESUME."""
    clock = FakeClock()
    monkeypatch.setattr(measurement_hold_mod, "_measurement_monotonic", clock.monotonic)

    async def guard(active: bool) -> None:
        if active:
            clock.now += guard_sec

    async def meter(_deadline: float) -> None:
        clock.now += meter_sec

    gate = _StuckGate(clock)
    wl = wake_loop_for_tests(
        output_gate=gate,
        volume_coordinator=_Volume(guard),
        tts=FakeTts(on_meter_pause=meter),
    )
    async with _serving(wl, short_sock_path):
        assert await _voice_uds_command(
            short_sock_path, "MEASURE_PAUSE", timeout=VOICE_MEASURE_PAUSE_TIMEOUT_SEC,
        ) == {"result": "ok", "drained": False}
        assert gate.waits == [pytest.approx(drain_bound)]
        assert clock.now == pytest.approx(guard_sec + meter_sec + drain_bound)
        assert wl._measurement_active.is_set()
        assert gate.admission_paused
        (record,) = event_records(caplog, "measurement.inflight_drain_timeout")
        assert record.levelno == logging.WARNING
        assert event_fields(caplog, "measurement.inflight_drain_timeout")[
            "active_kind"
        ] == "proactive"

        assert await _voice_uds_command(short_sock_path, "MEASURE_RESUME") == {
            "result": "ok",
        }
        assert not wl._measurement_active.is_set()
        assert not gate.admission_paused


async def test_pause_drains_only_when_opening_over_playing_output() -> None:
    """A busy session refuses, an idle opening has nothing to wait for, and a
    lease renewal (re-sent every MEASUREMENT_LEASE_REFRESH_SEC) stays
    latency-free even with output somehow playing."""
    gate = _StuckGate()
    wl = wake_loop_for_tests(output_gate=gate)
    wl._turns.state = State.SESSION
    assert await wl.measurement_hold.pause_response() == {"result": "BUSY"}
    assert not wl._measurement_active.is_set()

    wl._turns.state = State.WAKE
    gate.is_active = False
    assert await wl.measurement_hold.pause_response() == {"result": "ok", "drained": True}
    assert wl._measurement_active.is_set()
    gate.is_active = True
    assert await wl.measurement_hold.pause_response() == {"result": "ok", "drained": True}

    assert gate.waits == []
    await wl.measurement_hold.resume()


# --- setup failure, rollback and resume ------------------------------------


@pytest.mark.parametrize("phase", ["volume_guard", "content_meter"])
@pytest.mark.parametrize("error_type", [RuntimeError, AssertionError, SystemExit])
async def test_setup_failure_after_opening_rolls_back_once(
    phase: str, error_type: type[BaseException],
) -> None:
    """Crash recovery is armed before the first external await, and
    completion — not an exception allowlist — owns rollback: admission
    reopens exactly once, and a meter that never paused is not resumed."""
    armed: list[tuple[bool, bool, bool]] = []
    safeties: list[asyncio.Task | None] = []

    async def fail_in(failing_phase: str) -> None:
        if failing_phase != phase:
            return
        safety = wl.measurement_hold._safety_task
        safeties.append(safety)
        armed.append((
            wl._measurement_active.is_set(),
            gate.admission_paused,
            safety is not None and not safety.done(),
        ))
        raise error_type("setup failed")

    async def guard(active: bool) -> None:
        if active:
            await fail_in("volume_guard")

    async def meter(_deadline: float) -> None:
        await fail_in("content_meter")

    gate = _SpyGate()
    tts = FakeTts(on_meter_pause=meter)
    wl = wake_loop_for_tests(output_gate=gate, tts=tts, volume_coordinator=_Volume(guard))

    with pytest.raises(error_type):
        await wl.measurement_hold.pause_response()

    assert armed == [(True, True, True)]
    assert safeties[0] is not None and safeties[0].done()
    assert wl.measurement_hold._safety_task is None
    assert not wl._measurement_active.is_set()
    assert not gate.admission_paused
    assert gate.resume_calls == 1
    assert tts.meter_resumes == 0


async def test_uds_setup_expiry_rolls_back_inside_the_declared_total(
    monkeypatch, short_sock_path: str,
) -> None:
    """The wire answers non-ok only after local rollback, inside the total."""
    clock = FakeClock()
    monkeypatch.setattr(measurement_hold_mod, "_measurement_monotonic", clock.monotonic)

    async def guard(active: bool) -> None:
        clock.now += 0.40 if active else 0.10

    async def expiring_meter(deadline: float) -> None:
        clock.now = deadline
        raise TimeoutError("declared meter setup maximum reached")

    tts = FakeTts(on_meter_pause=expiring_meter)
    wl = wake_loop_for_tests(volume_coordinator=_Volume(guard), tts=tts)
    async with _serving(wl, short_sock_path):
        assert await _voice_uds_command(
            short_sock_path, "MEASURE_PAUSE", timeout=VOICE_MEASURE_PAUSE_TIMEOUT_SEC,
        ) == {"result": "ERROR"}
        assert clock.now == pytest.approx(MEASUREMENT_PAUSE_SETUP_DRAIN_TIMEOUT_SEC + 0.10)
        assert clock.now < MEASUREMENT_PAUSE_TOTAL_TIMEOUT_SEC
        assert not wl._measurement_active.is_set()
        assert not wl._output_gate.admission_paused
        # The measurement-specific meter pause, never the plain one.
        assert (tts.meter_pauses, tts.meter_resumes) == (0, 0)


async def test_uds_poisoned_meter_fails_closed_then_reconnects_on_next_access(
    monkeypatch, short_sock_path: str,
) -> None:
    """MEASURE_PAUSE never reconnects; a later ordinary control does once."""
    parent, child = socket.socketpair()
    poisoned = tts_mod._OutputdStreamAdapter(parent)
    poisoned.close()
    child.close()
    tts = TtsPlayout()
    tts._stream = poisoned  # type: ignore[assignment]
    replacement = FakeOutputdStream()
    connect = AsyncMock(return_value=replacement)
    monkeypatch.setattr(tts, "_connect_stream_adapter", connect)
    wl = wake_loop_for_tests(tts=tts)
    async with _serving(wl, short_sock_path):
        assert await _voice_uds_command(
            short_sock_path, "MEASURE_PAUSE", timeout=VOICE_MEASURE_PAUSE_TIMEOUT_SEC,
        ) == {"result": "ERROR"}
        connect.assert_not_awaited()
        assert tts._stream is poisoned
        assert not wl._measurement_active.is_set()
        assert not wl._output_gate.admission_paused

        await tts.pause_content_meter()
        connect.assert_awaited_once()
        assert tts._stream is replacement
        assert replacement.meter_pauses == 1


async def test_repeated_cancellation_waits_for_local_pause_rollback() -> None:
    meter_resume = _Hold("pause rollback")
    gate = _SpyGate(hold_drain=True)
    episode = await gate.begin_if_idle("admin")
    assert episode is not None
    wl = wake_loop_for_tests(output_gate=gate, tts=FakeTts(on_meter_resume=meter_resume))
    pause = asyncio.create_task(wl.measurement_hold.pause_response())
    await wait_signalled(gate.drain_entered, "measurement drain", producer=pause)

    pause.cancel()
    await wait_signalled(meter_resume.started, "measurement rollback", producer=pause)
    assert not wl._measurement_active.is_set()
    assert not gate.admission_paused

    pause.cancel()
    await asyncio.sleep(0)
    pause.cancel()
    await settle(5)
    assert not pause.done()

    meter_resume.release.set()
    with pytest.raises(asyncio.CancelledError):
        await pause
    await gate.end(episode)


async def test_resume_reopens_admission_before_stuck_meter_recovers() -> None:
    """Admission reopens before the mic ungates and before meter IPC, so a
    stuck outputd socket cannot leave a wake heard but its chirp refused
    (non-negotiable 6)."""
    meter_resume = _Hold("meter resume")
    wl = wake_loop_for_tests(tts=FakeTts(on_meter_resume=meter_resume))
    assert (await wl.measurement_hold.pause_response())["result"] == "ok"

    resume = asyncio.create_task(wl.measurement_hold.resume())
    await wait_signalled(meter_resume.started, "measurement meter resume", producer=resume)
    assert not resume.done()
    assert not wl._measurement_active.is_set()
    assert not wl._output_gate.admission_paused

    meter_resume.release.set()
    assert await resume == "ok"


async def test_resume_restores_after_safety_join_timeout(monkeypatch, caplog) -> None:
    monkeypatch.setattr(measurement_hold_mod, "MEASUREMENT_SAFETY_JOIN_TIMEOUT_SEC", 0.01)
    safety_started = asyncio.Event()
    release_safety = asyncio.Event()
    cancellation_seen = asyncio.Event()

    async def stubborn_safety() -> None:
        safety_started.set()
        try:
            await wait_signalled(release_safety, "release stubborn safety")
        except asyncio.CancelledError:
            cancellation_seen.set()
            await wait_signalled(release_safety, "release cancelled stubborn safety")

    wl = wake_loop_for_tests()
    await wl._output_gate.pause_admission()
    wl.measurement_hold._set_active_local(True, trigger="test")
    safety = asyncio.create_task(stubborn_safety())
    wl.measurement_hold._safety_task = safety
    await wait_signalled(safety_started, "stubborn measurement safety start", producer=safety)

    assert await wl.measurement_hold.resume() == "ok"
    assert cancellation_seen.is_set()
    assert not wl._measurement_active.is_set()
    assert not wl._output_gate.admission_paused
    assert len(event_records(caplog, "measurement.safety_join_timeout")) == 1

    release_safety.set()
    await asyncio.wait_for(safety, timeout=1.0)


async def test_measurement_deadline_cleanup_preserves_cancelled_error() -> None:
    """The shared capture boundary does not convert deadline cancellation."""

    cancellation = asyncio.CancelledError("measurement cleanup cancelled")

    async def cancelled_step() -> None:
        raise cancellation

    with pytest.raises(asyncio.CancelledError) as caught:
        await MeasurementHold._restore_step_before_deadline(
            cancelled_step(),
            deadline_monotonic=asyncio.get_running_loop().time() + 1.0,
            event="measurement.test_cleanup_failed",
            trigger="test",
        )

    assert caught.value is cancellation


# --- the lease and its timing contract -------------------------------------


async def test_lease_refresh_joins_stale_auto_clear_before_return(monkeypatch) -> None:
    """An expiring old lease cannot reopen admission behind its renewal."""
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
        measurement_hold_mod, "_measurement_safety_sleep", controlled_safety_sleep,
    )
    wl = wake_loop_for_tests()
    assert (await wl.measurement_hold.pause_response())["result"] == "ok"
    await wait_signalled(old_sleeping, "old measurement safety sleep")
    old_task = wl.measurement_hold._safety_task
    assert old_task is not None

    await wl.measurement_hold._transition_lock.acquire()
    try:
        renewal = asyncio.create_task(wl.measurement_hold.pause_response())
        await asyncio.sleep(0)
        release_old.set()
        await wait_signalled(old_released, "old measurement safety expiry", producer=old_task)
        await asyncio.sleep(0)
    finally:
        wl.measurement_hold._transition_lock.release()

    assert (await renewal)["result"] == "ok"
    await wait_signalled(new_sleeping, "renewed measurement safety sleep")
    assert old_task.done()
    assert wl.measurement_hold._safety_task is not old_task
    assert wl._measurement_active.is_set()
    assert wl._output_gate.admission_paused
    await wl.measurement_hold.resume()


async def test_renewal_timeout_releases_lock_for_auto_clear(monkeypatch, caplog) -> None:
    """An expiring setup cannot starve the generation-bound backstop."""
    clock = FakeClock()
    monkeypatch.setattr(measurement_hold_mod, "_measurement_monotonic", clock.monotonic)
    safety_sleep = _Hold("renewed measurement lease expiry")
    monkeypatch.setattr(measurement_hold_mod, "_measurement_safety_sleep", safety_sleep)

    async def expiring_guard(active: bool) -> None:
        if active:
            clock.now += MEASUREMENT_PAUSE_SETUP_DRAIN_TIMEOUT_SEC
            raise TimeoutError("volume setup exhausted aggregate budget")

    wl = wake_loop_for_tests(volume_coordinator=_Volume(expiring_guard))
    await wl._output_gate.pause_admission()
    wl.measurement_hold._set_active_local(True, trigger="test")

    with pytest.raises(TimeoutError):
        await wl.measurement_hold.pause_response()
    # The stub's own TimeoutError satisfies `raises`; only the wrapper logs this.
    assert event_fields(caplog, "measurement.pause_timeout")["phase"] == "volume_guard"
    safety = wl.measurement_hold._safety_task
    assert safety is not None
    await wait_signalled(safety_sleep.started, "renewed safety task", producer=safety)
    safety_sleep.release.set()
    await safety

    assert not wl._measurement_active.is_set()
    assert not wl._output_gate.admission_paused


def test_aggregate_pause_budget_fits_under_coordinator_read_timeout() -> None:
    """OLD coordinator + NEW daemon: install.sh restarts jasper-voice and
    jasper-web at different points of a deploy, so a coordinator pinned to
    the timeout it shipped with can be awaiting a daemon that holds the reply
    through a drain. A coordinator that gives up believes voice was never
    paused, skips MEASURE_RESUME, and leaves the speaker gated until the
    auto-clear — so the budget fits under that timeout with room for
    connect/write/scheduling on a loaded Pi."""
    assert MEASUREMENT_PAUSE_REPLY_MARGIN_SEC > 0
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
    assert MEASUREMENT_INFLIGHT_DRAIN_SEC <= MEASUREMENT_PAUSE_SETUP_DRAIN_TIMEOUT_SEC
    # Fan-in can hold 1.2 s ahead, plus one 250 ms chunk and the 85 ms
    # physical-tail allowance. Keep Pi scheduler headroom above that 1.535 s.
    assert MEASUREMENT_INFLIGHT_DRAIN_SEC >= 1.7


def test_lease_refresh_fits_under_the_daemon_measurement_auto_clear() -> None:
    """The coordinator keeps a long window alive by re-sending MEASURE_PAUSE
    every MEASUREMENT_LEASE_REFRESH_SEC; the daemon's crash backstop clears a
    window it has not heard about in MEASUREMENT_AUTOCLEAR_SEC. Invert that
    and a healthy sweep un-gates itself mid-capture.

    The budget is a whole failed-renewal cycle: the failed attempt waits up to
    VOICE_MEASURE_PAUSE_TIMEOUT_SEC before the loop switches to the retry
    interval, and the retry waits that long again (`_refresh_voice_lease` in
    measurement_window.py)."""
    assert 0 < MEASUREMENT_LEASE_REFRESH_SEC < MEASUREMENT_AUTOCLEAR_SEC
    assert (
        MEASUREMENT_LEASE_REFRESH_SEC
        + 2 * VOICE_MEASURE_PAUSE_TIMEOUT_SEC
        + MEASUREMENT_LEASE_RETRY_SEC
        < MEASUREMENT_AUTOCLEAR_SEC
    )


# --- output episodes hold through their physical tail ----------------------


async def test_pause_waits_for_physical_mute_click_tail() -> None:
    drain = _Hold("mute click tail")
    wl = wake_loop_for_tests(tts=FakeTts(on_drain=drain))
    click = asyncio.create_task(wl._play_mute_click(going_on=True))
    await wait_signalled(drain.started, "mute click physical drain", producer=click)

    pause = asyncio.create_task(wl.measurement_hold.pause_response())
    await settle(5)
    assert not pause.done()
    assert wl._output_gate.active_kind == "feedback"

    drain.release.set()
    await click
    assert await asyncio.wait_for(pause, timeout=1.0) == {"result": "ok", "drained": True}
    await wl.measurement_hold.resume()


async def test_partial_mute_write_keeps_gate_until_accepted_prefix_drains(
    monkeypatch,
) -> None:
    """A later AUDIO failure cannot erase an earlier command's audible tail."""
    monkeypatch.setattr(tts_mod, "_OUTPUTD_MAX_AUDIO_CHUNK_BYTES", 8)
    monkeypatch.setattr(tts_mod, "upsample_2x", lambda arr: arr)
    drain = _Hold("accepted-prefix drain")

    async def drain_sleep(seconds: float) -> None:
        assert seconds > 0
        await drain()

    # Only the drain's sleep is intercepted; the rest is the real asyncio.
    monkeypatch.setattr(tts_mod, "asyncio", SimpleNamespace(
        CancelledError=asyncio.CancelledError,
        Lock=asyncio.Lock,
        create_task=asyncio.create_task,
        current_task=asyncio.current_task,
        shield=asyncio.shield,
        to_thread=asyncio.to_thread,
        wait=asyncio.wait,
        sleep=drain_sleep,
    ))

    def fail_second_write(_data: bytes) -> None:
        if tts._stream.write_attempts == 2:
            raise OSError("second AUDIO command failed")

    wl, tts = _mute_click_rig(fail_second_write)
    click = asyncio.create_task(wl._play_mute_click(going_on=True))
    await wait_signalled(drain.started, "partial mute write accepted-prefix drain", producer=click)
    assert tts._stream.write_attempts == 2
    assert wl._output_gate.active_kind == "feedback"

    pause = asyncio.create_task(wl.measurement_hold.pause_response())
    await settle(5)
    assert not pause.done()

    drain.release.set()
    await click
    assert await asyncio.wait_for(pause, timeout=1.0) == {"result": "ok", "drained": True}
    await wl.measurement_hold.resume()


async def test_cancelled_mute_write_waits_for_acceptance_and_physical_tail() -> None:
    """Cancellation cannot outrun an uncancellable socket-write worker."""
    write_started, release_write, write_returned = (threading.Event() for _ in range(3))

    def block_write(_data: bytes) -> None:
        write_started.set()
        if not release_write.wait(timeout=2.0):
            raise TimeoutError("test did not release AUDIO write")
        write_returned.set()

    wl, tts = _mute_click_rig(block_write)
    click = asyncio.create_task(wl._play_mute_click(going_on=True))
    assert await asyncio.to_thread(write_started.wait, 1.0)
    click.cancel()
    await settle(5)
    assert not click.done()
    assert wl._output_gate.active_kind == "feedback"

    pause = asyncio.create_task(wl.measurement_hold.pause_response())
    await settle(5)
    assert not pause.done()

    release_write.set()
    assert await asyncio.to_thread(write_returned.wait, 1.0)
    while tts._ring_end_monotonic is None:
        await asyncio.sleep(0)
    drain_at = tts.expected_drain_at()
    click.cancel()
    await settle(5)
    assert not click.done(), "accepted AUDIO tail must still hold feedback gate"
    assert not pause.done(), "PAUSE must still be draining feedback ownership"
    assert asyncio.get_running_loop().time() < drain_at

    with pytest.raises(asyncio.CancelledError):
        await click
    assert asyncio.get_running_loop().time() >= drain_at
    assert await asyncio.wait_for(pause, timeout=1.0) == {"result": "ok", "drained": True}
    await wl.measurement_hold.resume()


@pytest.mark.parametrize("path", ["admin", "proactive"])
async def test_cancelled_cue_keeps_episode_and_duck_until_physical_tail(
    monkeypatch, tmp_path, path: str,
) -> None:
    """Accepted cue PCM keeps its episode, and the duck over it, through
    repeated cancellation: the duck restores only once the tail has drained,
    the episode is released exactly once after that, and a PAUSE waits for
    all of it."""
    monkeypatch.setattr(tts_mod, "upsample_2x", lambda arr: arr)
    drain = _Hold("accepted cue tail")
    tts = _playout(drain_tail_sec=0.0)
    tts.wait_drained = drain  # type: ignore[method-assign]
    if path == "admin":
        cues = AudioCueManager(
            sounds_dir=str(tmp_path), hostname="jts.local", voice="Aoede", tts_playout=tts,
        )
        cue = find("cant_connect")
        assert cue is not None
        with wave.open(cues.expected_path(cue), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(_CUE_PCM)
    else:
        cues = _SpeakingCues(tts)
    ducker = _Ducker(hold_restore=True)
    gate = _SpyGate()
    wl = wake_loop_for_tests(output_gate=gate, tts=tts, cues=cues, ducker=ducker)
    playing = asyncio.create_task(
        wl._play_cue("cant_connect") if path == "admin"
        else wl._play_dynamic_text("Timer finished")
    )
    await wait_signalled(drain.started, f"{path} cue physical drain", producer=playing)
    assert tts._ring_end_monotonic is not None
    pause = asyncio.create_task(wl.measurement_hold.pause_response())

    playing.cancel()
    await asyncio.sleep(0)
    playing.cancel()
    await settle(5)
    assert not playing.done()
    assert not pause.done()
    assert ducker.restore_calls == 0, "the duck must cover the accepted tail"
    assert gate.active_kind == path

    drain.release.set()
    await wait_signalled(ducker.restore_hold.started, f"{path} duck restore", producer=playing)
    playing.cancel()
    await settle(5)
    assert not playing.done(), "a cancel must not interrupt the restore"
    assert not ducker.restored
    assert gate.active_kind == path

    ducker.restore_hold.release.set()
    with pytest.raises(asyncio.CancelledError):
        await playing
    assert await asyncio.wait_for(pause, timeout=1.0) == {"result": "ok", "drained": True}
    assert ducker.restored
    assert (ducker.duck_calls, ducker.restore_calls, gate.end_calls) == (1, 1, 1)
    assert not gate.is_active
    await wl.measurement_hold.resume()


@pytest.mark.parametrize(
    ("drain_error", "restore_error", "raised"),
    [
        (RuntimeError("drain exploded"), ValueError("restore exploded"), None),
        (_Abort("drain aborted"), None, "drain"),
        (_Abort("drain aborted"), _Abort("restore aborted"), "restore"),
    ],
)
async def test_duck_cleanup_restores_and_releases_once_whatever_fails(
    caplog,
    drain_error: BaseException,
    restore_error: BaseException | None,
    raised: str | None,
) -> None:
    """Drain and restore Exceptions are both logged, never raised; a
    BaseException propagates, the restore's taking precedence over the
    drain's. Either way the restore runs and the episode is released once."""

    async def drain() -> None:
        raise drain_error

    restores: list[None] = []

    async def restore() -> None:
        restores.append(None)
        if restore_error is not None:
            raise restore_error

    gate = _SpyGate()
    wl = wake_loop_for_tests(output_gate=gate, tts=FakeTts(on_drain=drain))
    episode = await gate.begin_if_idle("admin")
    assert episode is not None
    cleanup = wl._assistant_output.finish_ducked_episode_after_drain(
        episode, restore, cleanup_label="contract cue",
    )
    if raised is None:
        await cleanup
        assert [
            (r.levelno, type(r.args[1]))
            for r in caplog.records
            if isinstance(r.args, tuple) and r.args[:1] == ("contract cue",)
        ] == [(logging.WARNING, RuntimeError), (logging.WARNING, ValueError)]
    else:
        with pytest.raises(_Abort) as caught:
            await cleanup
        assert caught.value is (restore_error if raised == "restore" else drain_error)
    assert len(restores) == 1
    assert gate.end_calls == 1
    assert not gate.is_active


@pytest.mark.parametrize("path", ["admin", "proactive"])
@pytest.mark.parametrize("on_outcome", ["true", "false", "error"])
async def test_cancelled_fanin_duck_on_lands_then_cleanup_sends_off(
    path: str, on_outcome: str,
) -> None:
    """Cancellation cannot orphan any possibly-delivered remote ON."""
    remote = _RemoteDuck(on_outcome)
    ducker = FanInDucker(remote)  # type: ignore[arg-type]
    cues = SpyCues()
    gate = _SpyGate()
    wl = wake_loop_for_tests(output_gate=gate, ducker=ducker, cues=cues)
    playing = asyncio.create_task(
        wl._play_cue("cant_connect") if path == "admin"
        else wl._play_dynamic_text("Timer finished")
    )
    try:
        await wait_signalled(remote.started[True], f"{path} PROGRAM_DUCK_ON", producer=playing)
        await _cancel_through_remote_duck(playing, remote, ducker, gate, path)
    finally:
        remote.release_all()
    assert (cues.played, cues.spoken) == ([], [])


@pytest.mark.parametrize("on_outcome", ["false", "error"])
async def test_fanin_ambiguous_on_owns_off_without_changing_original_semantics(
    on_outcome: str,
) -> None:
    """False still returns and an unexpected error still propagates."""
    remote = _RemoteDuck(on_outcome, hold=False)
    ducker = FanInDucker(remote)  # type: ignore[arg-type]

    if on_outcome == "false":
        await ducker.duck()
    else:
        with pytest.raises(RuntimeError) as caught:
            await ducker.duck()
        assert caught.value is remote.error

    assert ducker.is_ducked
    await ducker.restore()
    assert not ducker.is_ducked
    assert remote.commands == [True, False]


# --- a failed or cancelled turn begin owns its cleanup ---------------------


@pytest.mark.parametrize("on_outcome", ["true", "false", "error"])
async def test_cancelled_begin_turn_owns_full_cleanup_through_fanin_off(
    monkeypatch, on_outcome: str,
) -> None:
    """A cancelled real begin resets once after every ambiguous ON result."""
    remote = _RemoteDuck(on_outcome)
    ducker = FanInDucker(remote)  # type: ignore[arg-type]
    tts, content, volume, usage, gate = (
        FakeTts(), _Content(), _Volume(), FakeUsageStore(), _SpyGate(),
    )
    wl = wake_loop_for_tests(
        ducker=ducker,
        tts=tts,
        volume_coordinator=volume,
        output_gate=gate,
        content_activity=content,
        usage_store=usage,
    )
    turn = FakeLiveTurn()
    monkeypatch.setattr(wl._connection, "acquire_turn", AsyncMock(return_value=turn))
    cleanups = _count_failed_begin_cleanups(wl, monkeypatch)
    beginning = asyncio.create_task(wl._begin_turn(pre_roll=False))
    try:
        await wait_signalled(remote.started[True], "turn PROGRAM_DUCK_ON", producer=beginning)
        assert volume.session_calls == [True]
        assert content.pause_calls == 1
        assert tts.meter_pauses == 1
        await _cancel_through_remote_duck(beginning, remote, ducker, gate, "turn")
    finally:
        remote.release_all()

    assert len(cleanups) == 1
    _assert_turn_released(wl, gate)
    assert len(tts.prepares) == 1
    assert (tts.meter_pauses, tts.meter_resumes) == (1, 1)
    assert (content.refresh_calls, content.pause_calls, content.resume_calls) == (0, 1, 1)
    assert volume.session_calls == [True, False]
    assert (usage.open_calls, usage.close_calls) == (1, 1)
    assert turn.release_calls == 1


async def test_begin_turn_preserves_base_exception_after_owned_cleanup(
    monkeypatch,
) -> None:
    """Failed preparation releases its resources without cancelling old teardown."""
    failure = _Abort("begin aborted")
    content, gate = _Content(), _SpyGate()
    wl = wake_loop_for_tests(output_gate=gate, content_activity=content)
    release_started, finish_release = asyncio.Event(), asyncio.Event()

    async def prior_release() -> None:
        release_started.set()
        await finish_release.wait()

    async def fail_prepare() -> None:
        await release_started.wait()
        await asyncio.sleep(0)
        raise failure

    releasing = asyncio.create_task(prior_release())
    wl._turns.pending_release = releasing
    acquire = AsyncMock()
    monkeypatch.setattr(wl._connection, "acquire_turn", acquire)
    monkeypatch.setattr(wl._assistant_output, "prepare_loudness", fail_prepare)
    cleanups = _count_failed_begin_cleanups(wl, monkeypatch)

    try:
        with pytest.raises(_Abort) as caught:
            await wl._begin_turn(pre_roll=False)

        assert caught.value is failure
        assert len(cleanups) == 1
        assert content.resume_calls == 1
        _assert_turn_released(wl, gate)
        assert wl._turns.pending_release is releasing
        assert not releasing.done()
        acquire.assert_not_awaited()
    finally:
        finish_release.set()
        assert wl._turns.take_pending_release() is releasing
        await releasing


async def test_repeated_begin_cancellation_waits_for_provider_teardown(monkeypatch) -> None:
    connecting, cleanup_started, finish_cleanup = (
        asyncio.Event(), asyncio.Event(), asyncio.Event(),
    )
    gate = _SpyGate()
    tts = FakeTts()
    wl = wake_loop_for_tests(output_gate=gate, tts=tts)
    cleanup_finished = False

    async def acquire_turn():
        nonlocal cleanup_finished
        connecting.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await finish_cleanup.wait()
            cleanup_finished = True

    monkeypatch.setattr(wl._connection, "acquire_turn", acquire_turn)
    beginning = asyncio.create_task(wl._begin_turn(pre_roll=False))
    try:
        await wait_signalled(connecting, "provider connection", producer=beginning)
        beginning.cancel("initial cancellation")
        await wait_signalled(cleanup_started, "provider cleanup", producer=beginning)
        beginning.cancel("repeated cancellation")
        await settle(5)
        assert not beginning.done()
        assert gate.active_kind == "turn"
        assert tts.meter_resumes == 0

        finish_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await beginning
        assert beginning.cancelled()
        assert cleanup_finished
        assert tts.meter_resumes == 1
        _assert_turn_released(wl, gate)
    finally:
        finish_cleanup.set()
        beginning.cancel()
        await asyncio.gather(beginning, return_exceptions=True)


@pytest.mark.parametrize("path", ["wake", "manual"])
@pytest.mark.parametrize("acquired", [False, True])
async def test_cancelled_listening_feedback_prepare_owns_cleanup(
    monkeypatch, path: str, acquired: bool,
) -> None:
    """Wake and manual prefixes retain their episode through repeated cancel."""
    prepare = _Hold("listening-feedback loudness preparation")
    acquire_started, acquire_finished = asyncio.Event(), asyncio.Event()
    turn = FakeLiveTurn()

    async def acquire_turn():
        acquire_started.set()
        try:
            if not acquired:
                await asyncio.Event().wait()
            return turn
        finally:
            acquire_finished.set()

    ducker = _Ducker(hold_restore=True)
    tts, content, volume, gate = FakeTts(), _Content(), _Volume(), _SpyGate()
    wl = wake_loop_for_tests(
        ducker=ducker,
        tts=tts,
        volume_coordinator=volume,
        output_gate=gate,
        content_activity=content,
    )
    monkeypatch.setattr(wl._assistant_output, "prepare_loudness", prepare)
    monkeypatch.setattr(wl._connection, "acquire_turn", acquire_turn)
    cleanups = _count_failed_begin_cleanups(wl, monkeypatch)

    if path == "wake":
        async def win_arbitration(**_kwargs) -> str:
            return "WIN"

        wl._wake_late_cancelled = lambda *_args: False
        wl._peering.arbitrate = win_arbitration
        wl._acquiring = True
        beginning = asyncio.create_task(wl._arbitrate_acquire_drain(
            score=0.9,
            rms_dbfs=-30.0,
            spend_allowed=True,
            conn_paused=False,
            can_serve=True,
        ))
    else:
        beginning = asyncio.create_task(wl.manual_session_start())

    await wait_signalled(prepare.started, f"{path} loudness preparation", producer=beginning)
    await wait_signalled(acquire_started, "parallel provider acquisition", producer=beginning)
    assert gate.active_kind == "turn"

    beginning.cancel("original prefix cancellation")
    await wait_signalled(
        ducker.restore_hold.started, f"{path} failed-begin restore", producer=beginning,
    )
    assert gate.active_kind == "turn"

    beginning.cancel("repeat cleanup cancellation")
    await settle(5)
    assert not beginning.done()
    assert gate.active_kind == "turn"

    ducker.restore_hold.release.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await beginning

    assert caught.value.args == ("original prefix cancellation",)
    assert beginning.cancelled()
    assert len(cleanups) == 1
    _assert_turn_released(wl, gate)
    assert (ducker.duck_calls, ducker.restore_calls) == (0, 1)
    assert tts.meter_resumes == 1
    assert (content.pause_calls, content.resume_calls) == (1, 1)
    assert acquire_finished.is_set()
    assert turn.release_calls == int(acquired)
    assert volume.session_calls == [False]


@pytest.mark.parametrize("listening_feedback", [False, True])
@pytest.mark.parametrize("first", ["output", "connection", "prepare_error"])
async def test_begin_turn_overlaps_one_output_preparation_with_connection(
    monkeypatch,
    listening_feedback: bool,
    first: str,
) -> None:
    prepare_started, prepared = asyncio.Event(), asyncio.Event()
    connect_started, connected = asyncio.Event(), asyncio.Event()
    release_prepare, release_connect = asyncio.Event(), asyncio.Event()
    chirped = asyncio.Event()
    gate = _SpyGate()
    tts = FakeTts()
    turn = FakeLiveTurn()
    wl = wake_loop_for_tests(output_gate=gate, tts=tts)
    real_prepare = tts.prepare_assistant_context

    async def prepare(**kwargs) -> None:
        assert gate.active_kind == "turn"
        prepare_started.set()
        await release_prepare.wait()
        if first == "prepare_error":
            raise RuntimeError("output context unavailable")
        await real_prepare(**kwargs)
        prepared.set()

    async def connect():
        connect_started.set()
        await release_connect.wait()
        connected.set()
        return turn

    async def chirp(*, going_on: bool) -> None:
        assert going_on
        assert len(tts.prepares) == 1
        assert gate.active_kind == "turn"
        chirped.set()

    async def audio_out_chunks():
        await asyncio.Event().wait()
        yield  # pragma: no cover

    monkeypatch.setattr(tts, "prepare_assistant_context", prepare)
    monkeypatch.setattr(wl._connection, "acquire_turn", connect)
    monkeypatch.setattr(wl, "_play_listening_chirp", chirp)
    monkeypatch.setattr(turn, "audio_out_chunks", audio_out_chunks)
    beginning = asyncio.create_task(wl._begin_turn(
        pre_roll=False, listening_feedback=listening_feedback,
    ))
    try:
        await wait_signalled(prepare_started, "speaker preparation", producer=beginning)
        await wait_signalled(connect_started, "parallel provider connection", producer=beginning)
        assert not chirped.is_set()
        if first == "output":
            release_prepare.set()
            await wait_signalled(prepared, "prepared context", producer=beginning)
            if listening_feedback:
                await wait_signalled(chirped, "feedback while connecting", producer=beginning)
        else:
            release_connect.set()
            await wait_signalled(connected, "connected provider", producer=beginning)
            assert not chirped.is_set()
        assert not beginning.done()
        release_prepare.set()
        release_connect.set()
        if first == "prepare_error":
            with pytest.raises(RuntimeError):
                await beginning
            assert turn.release_calls == 1
            _assert_turn_released(wl, gate)
        else:
            await beginning
            assert wl._turns.state is State.SESSION
            assert len(tts.prepares) == 1
            assert tts.meter_pauses == 1
            assert chirped.is_set() is listening_feedback
    finally:
        beginning.cancel()
        await asyncio.gather(beginning, return_exceptions=True)
        await wl._turns.cleanup_after_failed_begin()


@pytest.mark.parametrize(
    ("failing", "error", "event", "field", "value"),
    [
        ("duck", RuntimeError("duck restore failed"),
         "turn.output_cleanup_failed", "phase", "duck_restore"),
        ("meter", RuntimeError("meter resume failed"),
         "turn.output_cleanup_failed", "phase", "content_meter_resume"),
        ("usage", RuntimeError("usage close failed"),
         "turn.begin_cleanup_phase_failed", "phase", "usage_session_close"),
        ("duck", _Abort("duck restore aborted"),
         "turn.begin_cleanup_failed", "exc_type", "_Abort"),
    ],
)
async def test_failed_begin_cleanup_runs_every_phase_after_phase_failure(
    caplog,
    failing: str,
    error: BaseException,
    event: str,
    field: str,
    value: str,
) -> None:
    """No ordinary or non-ordinary cleanup failure can wedge later phases."""
    begin_error = RuntimeError("original turn begin failure")

    async def failed_inner(**_kwargs) -> None:
        raise begin_error

    async def meter_resume() -> None:
        if failing == "meter":
            raise error

    turn = FakeLiveTurn()
    ducker = _Ducker(restore_error=error if failing == "duck" else None)
    volume, content, gate = _Volume(), _Content(), _SpyGate()
    tts = FakeTts(on_meter_resume=meter_resume)
    usage = FakeUsageStore(close_error=error if failing == "usage" else None)
    wl = wake_loop_for_tests(
        ducker=ducker,
        volume_coordinator=volume,
        tts=tts,
        output_gate=gate,
        content_activity=content,
        usage_store=usage,
    )
    wl._turns.output_episode = await gate.begin_turn()
    wl._turns.turn = turn
    wl._turns.session_id = 42
    wl._turns.bg_tasks = {asyncio.create_task(asyncio.sleep(60))}
    wl._turns._bg_end_scheduled = True
    wl._push_to_talk.active_source = "test_remote"
    wl._acquiring = True
    wl._turns.state = State.SESSION
    wl._wake_legs.refractory_until = -1.0
    wl._turns.begin_inner = failed_inner

    caplog.set_level(logging.WARNING, logger="jasper.voice_daemon")
    with pytest.raises(RuntimeError) as caught:
        await wl._begin_turn(pre_roll=False)

    assert caught.value is begin_error
    assert event_fields(caplog, event)[field] == value
    _assert_turn_released(wl, gate)
    assert wl._turns._bg_end_scheduled is False
    assert wl._wake_legs.refractory_until > 0.0
    assert turn.release_calls == 1
    assert ducker.restore_calls == 1
    assert volume.session_calls == [False]
    assert content.resume_calls == 1
    assert tts.meter_resumes == 1
    assert usage.close_calls == 1


async def test_failed_begin_drains_opening_feedback_without_completion_chirp():
    gate = _SpyGate()
    wl = wake_loop_for_tests(output_gate=gate)
    accepted, finish_write, draining, finish_drain = (asyncio.Event() for _ in range(4))
    writes = []

    async def write(pcm, **_kwargs):
        writes.append(pcm)
        accepted.set()
        await finish_write.wait()

    async def drain():
        draining.set()
        await finish_drain.wait()

    async def fail(**_kwargs):
        await accepted.wait()
        raise RuntimeError("acquisition failed")

    wl._tts.write_segment = write
    wl._tts.wait_drained = drain
    wl._ducker.restore = AsyncMock()
    wl._connection.acquire_turn = fail
    beginning = asyncio.create_task(wl._begin_turn(listening_feedback=True))
    try:
        await wait_signalled(accepted, "opening feedback", producer=beginning)
        await settle(5)
        assert not beginning.done()
        assert gate.active_kind == "turn"
        wl._ducker.restore.assert_not_awaited()
        finish_write.set()
        await wait_signalled(draining, "opening feedback drain", producer=beginning)
        assert not beginning.done()
        assert gate.active_kind == "turn"
        finish_drain.set()
        with pytest.raises(RuntimeError):
            await beginning
    finally:
        finish_write.set()
        finish_drain.set()
        await asyncio.gather(beginning, return_exceptions=True)

    assert writes == [wl._assistant_output._chirp_on_pcm]
    wl._ducker.restore.assert_awaited_once()
    _assert_turn_released(wl, gate)
