# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Assistant audio is refused for the whole room-correction measurement
window (issues #1786, #1898, #1913).

A window is opened and closed by `measurement_pause()` /
`measurement_resume()` (the coordinator's MEASURE_PAUSE/RESUME UDS
commands — see `jasper.measurement_window.measurement_window()`,
which the crossover-v2 flow holds open for a whole session via
`acquire_session_measurement_pause()`). Refusal happens at one
admission authority asked at two moments: `AssistantOutputGate`
refuses an episode that has not started, and the `TtsPlayout`
emission seam refuses the bytes of one that already had — so a task
that passed an earlier check, including a wake already in flight when
the pause landed, still cannot reach the capture.

These tests pin that refusal at both moments, the structured code that
names it, and normal playback once the window closes.

`announce_research_ready` keeps its own early check (a queued job is
held, not dropped); that coverage lives in
tests/test_voice_daemon_research_announce.py.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from jasper.audio_io import TtsPlayout
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
    assert await wl.measurement_pause() == "ok"

    with caplog.at_level(logging.INFO, logger="jasper.voice_daemon"):
        result = await wl.play_cue("cant_connect")

    assert result == "measurement_active"
    assert "event=cue.skipped" in caplog.text
    assert "reason=measurement_active" in caplog.text
    assert "slug=cant_connect" in caplog.text
    await wl.measurement_resume()


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


@pytest.mark.parametrize("output_busy", [False, True])
async def test_play_supervisor_cue_refuses_during_measurement(
    caplog, output_busy: bool,
) -> None:
    from jasper.voice_daemon import WakeLoop

    wl = WakeLoop.for_tests()
    wl._cues = _RefusingCues()
    if output_busy:
        assert await wl._output_gate.begin_if_idle("admin") is not None
    assert await wl.measurement_pause() == "ok"

    with caplog.at_level(logging.INFO, logger="jasper.voice_daemon"):
        result = await wl.play_supervisor_cue("cant_connect")

    assert result == "measurement_active"
    if not output_busy:
        assert "event=cue.skipped" in caplog.text
        assert "reason=measurement_active" in caplog.text
    await wl.measurement_resume()


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

    class _Cues:
        async def prerender_text(self, _text: str) -> bool:
            return True

        async def speak_text(self, _text: str) -> None:
            raise AssertionError(
                "timer must not speak during a measurement window"
            )

    wl = WakeLoop.for_tests()
    wl._cues = _Cues()
    assert await wl.measurement_pause() == "ok"

    with caplog.at_level(logging.INFO, logger="jasper.voice_daemon"):
        await wl.announce_timer(_timer())

    assert "event=dynamic_text.skipped" in caplog.text
    assert "reason=measurement_active" in caplog.text
    await wl.measurement_resume()


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


async def test_prerender_race_cannot_admit_timer_after_pause() -> None:
    """A timer past its early check is still stopped at atomic admission."""

    from jasper.voice_daemon import WakeLoop

    prerender_started = asyncio.Event()
    finish_prerender = asyncio.Event()
    spoke: list[str] = []

    class _Cues:
        async def prerender_text(self, _text: str) -> bool:
            prerender_started.set()
            await finish_prerender.wait()
            return True

        async def speak_text(self, text: str) -> None:
            spoke.append(text)

    wl = WakeLoop.for_tests()
    wl._cues = _Cues()
    announce = asyncio.create_task(wl.announce_timer(_timer()))
    await asyncio.wait_for(prerender_started.wait(), timeout=1.0)

    assert await wl.measurement_pause() == "ok"
    finish_prerender.set()
    await asyncio.wait_for(announce, timeout=1.0)

    assert spoke == []
    assert wl._output_gate.admission_paused
    await wl.measurement_resume()


async def test_measurement_pause_blocks_mute_click_admission() -> None:
    from jasper.voice_daemon import WakeLoop

    writes: list[bytes] = []
    wl = WakeLoop.for_tests()

    async def write_segment(pcm, **_kwargs):
        writes.append(pcm)

    wl._tts.write_segment = write_segment
    assert await wl.measurement_pause() == "ok"

    await wl._play_mute_click(going_on=True)

    assert writes == []
    await wl.measurement_resume()


class _RecordingTts(TtsPlayout):
    """The production emission seam over a recording transport."""

    def __init__(self) -> None:
        super().__init__()
        self.segments: list[bytes] = []

    async def _write_segment(self, pcm: bytes, **_kwargs) -> None:
        self.segments.append(pcm)

    async def pause_content_meter(self) -> None:
        return None

    async def pause_content_meter_for_measurement(
        self, deadline_monotonic: float,
    ) -> None:
        return None

    async def resume_content_meter(self) -> None:
        return None


async def test_wake_in_flight_when_pause_lands_cannot_emit(
    monkeypatch, caplog,
) -> None:
    """issue #1913: the wake owns output before PAUSE and outlives the
    bounded drain, so neither an earlier check nor the drain can stop its
    audio — only the emission seam can. Zeroing the drain bound makes the
    ordering exact (episode acquired, then pause, then emit) with no wait."""
    from jasper.voice_daemon import WakeLoop

    monkeypatch.setattr(
        "jasper.voice_daemon.MEASUREMENT_INFLIGHT_DRAIN_SEC", 0.0,
    )
    tts = _RecordingTts()
    wl = WakeLoop.for_tests(tts=tts)
    await wl._begin_turn_output_episode()

    assert await wl.measurement_pause() == "ok"

    with caplog.at_level(logging.INFO, logger="jasper.audio_io"):
        await wl._play_listening_chirp(going_on=True)
        await wl._tts.write_segment(b"\x00\x00", segment_kind="assistant")

    assert tts.segments == []
    assert "event=tts_write.refused" in caplog.text
    assert "reason=measurement_active" in caplog.text
    await wl.measurement_resume()


async def test_emission_proceeds_once_the_window_closes() -> None:
    from jasper.voice_daemon import WakeLoop

    tts = _RecordingTts()
    wl = WakeLoop.for_tests(tts=tts)

    assert await wl.measurement_pause() == "ok"
    await wl.measurement_resume()
    await wl._play_listening_chirp(going_on=True)

    assert tts.segments == [wl._chirp_on_pcm]
