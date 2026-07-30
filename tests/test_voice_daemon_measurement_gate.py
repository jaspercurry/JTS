# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Room-correction measurement gate for proactive cue/announce paths
(issue #1786).

`_measurement_active` is set for the duration of a room-correction
measurement sweep (`measurement_pause()` / `measurement_resume()`,
driven by the coordinator's MEASURE_PAUSE/RESUME UDS commands — see
`jasper.correction.coordinator.measurement_window()`, which the
crossover-v2 flow holds open for a whole session via
`acquire_session_measurement_pause()`). Several proactive/admin
playback paths checked only the assistant-output gate and/or session
state, not this flag, so a background cue or a timer firing mid-sweep
could speak into the capture and corrupt it.

These tests pin that `play_cue`, `play_supervisor_cue`, and
`announce_timer` all refuse to play while a measurement is active, log
a structured event naming the suppressed cue/timer and why, and behave
normally once the flag is clear.

`announce_research_ready`'s equivalent coverage — plus the adjacent
`_open_confirmation_window` fix for a job whose measurement window
opens between the "ready?" prompt and the confirmation attempt — lives
in tests/test_voice_daemon_research_announce.py, colocated with the
pre-existing research-announce guard-ladder tests it had to correct
(that ladder previously asserted a measurement-active job gets read
aloud immediately, which was itself the bug).
"""
from __future__ import annotations

import logging

from jasper.timers import Timer


def _timer(*, id: str = "t1", label: str | None = "pasta") -> Timer:
    return Timer(id=id, label=label, fire_at=0.0, total_seconds=60, created_at=0.0)


class _RefusingCues:
    """A cue manager that raises if asked to play — proves nothing played."""

    async def play(self, _slug: str) -> bool:
        raise AssertionError("cue must not play during a measurement window")


async def test_play_cue_refuses_during_measurement(caplog) -> None:
    from jasper.voice_daemon import WakeLoop

    wl = WakeLoop.for_tests()
    wl._cues = _RefusingCues()
    wl._measurement_active.set()

    with caplog.at_level(logging.INFO, logger="jasper.voice_daemon"):
        result = await wl.play_cue("cant_connect")

    assert result == "measurement_active"
    assert "event=cue.skipped" in caplog.text
    assert "reason=measurement_active" in caplog.text
    assert "slug=cant_connect" in caplog.text


async def test_play_cue_plays_normally_when_not_measuring() -> None:
    from jasper.voice_daemon import WakeLoop

    played: list[str] = []

    class _Cues:
        async def play(self, slug: str) -> bool:
            played.append(slug)
            return True

    wl = WakeLoop.for_tests()
    wl._cues = _Cues()

    assert await wl.play_cue("cant_connect") == "ok"
    assert played == ["cant_connect"]


async def test_play_supervisor_cue_refuses_during_measurement(caplog) -> None:
    from jasper.voice_daemon import WakeLoop

    wl = WakeLoop.for_tests()
    wl._cues = _RefusingCues()
    wl._measurement_active.set()

    with caplog.at_level(logging.INFO, logger="jasper.voice_daemon"):
        result = await wl.play_supervisor_cue("cant_connect")

    assert result == "skipped_measurement_active"
    assert "event=cue.skipped" in caplog.text
    assert "reason=measurement_active" in caplog.text


async def test_play_supervisor_cue_plays_normally_when_not_measuring() -> None:
    from jasper.voice_daemon import WakeLoop

    played: list[str] = []

    class _Cues:
        async def play(self, slug: str) -> bool:
            played.append(slug)
            return True

    wl = WakeLoop.for_tests()
    wl._cues = _Cues()

    assert await wl.play_supervisor_cue("cant_connect") == "ok"
    assert played == ["cant_connect"]


async def test_announce_timer_suppressed_during_measurement(caplog) -> None:
    from jasper.voice_daemon import WakeLoop

    async def _refuse(_text: str) -> bool:
        raise AssertionError("timer must not speak during a measurement window")

    wl = WakeLoop.for_tests()
    wl._measurement_active.set()
    wl._play_dynamic_text = _refuse

    with caplog.at_level(logging.INFO, logger="jasper.voice_daemon"):
        await wl.announce_timer(_timer())

    assert "event=timer.announce_suppressed" in caplog.text
    assert "reason=measurement_active" in caplog.text
    assert "timer_id=t1" in caplog.text


async def test_announce_timer_speaks_normally_when_not_measuring() -> None:
    from jasper.voice_daemon import WakeLoop

    spoken: list[str] = []

    async def _play(text: str) -> bool:
        spoken.append(text)
        return True

    wl = WakeLoop.for_tests()
    wl._play_dynamic_text = _play

    await wl.announce_timer(_timer())

    assert spoken == ["Your pasta timer is up."]


async def test_announce_timer_rechecks_measurement_after_grace_wait(monkeypatch) -> None:
    """A measurement window that opens while announce_timer is waiting
    out an output-busy condition must still be caught — the post-loop
    re-check (not just the up-front one) is what closes this race.

    Deterministic, no real sleeping: a stub `_output_gate.is_active`
    reports busy on its first read (entering the grace-wait loop), then
    flips to idle on the next read while arming `_measurement_active` as
    a side effect — simulating a measurement window opening in the same
    instant prior output clears. `asyncio.sleep` is stubbed so the loop's
    0.5 s poll doesn't cost real wall-clock time.
    """
    from jasper.voice_daemon import WakeLoop

    async def _refuse(_text: str) -> bool:
        raise AssertionError("timer must not speak during a measurement window")

    wl = WakeLoop.for_tests()
    wl._play_dynamic_text = _refuse

    class _OutputGateStub:
        def __init__(self) -> None:
            self.reads = 0

        @property
        def is_active(self) -> bool:
            self.reads += 1
            if self.reads == 1:
                return True
            wl._measurement_active.set()
            return False

    wl._output_gate = _OutputGateStub()

    async def _fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("jasper.voice_daemon.asyncio.sleep", _fake_sleep)

    await wl.announce_timer(_timer())
