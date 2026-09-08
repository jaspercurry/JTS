# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Assistant audio is refused for the whole room-correction measurement
window (issues #1786, #1898, #1913).

A window is opened and closed by `MeasurementHold.pause_response()` /
`MeasurementHold.resume()` (the coordinator's MEASURE_PAUSE/RESUME UDS
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
tests/test_research_announcer.py.
"""
from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import numpy as np

import pytest

from jasper.audio_io import TtsPlayout
from jasper.audio_buffer import InputFrame
from jasper.cues.manager import AudioCueManager
from jasper.timers import Timer
from tests._cue_spy import SpyCues
from tests._log_events import event_fields
from tests._wake_loop import wake_loop_for_tests


def _timer(*, id: str = "t1", label: str | None = "pasta") -> Timer:
    return Timer(id=id, label=label, fire_at=0.0, total_seconds=60, created_at=0.0)


class _RefusingCues(SpyCues):
    """A cue manager that raises if asked to play — proves nothing played.

    The rest of the surface is the shared spy's, so a gate that records its
    refusal on the manager still finds the method there."""

    async def play(self, _slug: str) -> bool:
        raise AssertionError("cue must not play during a measurement window")


async def test_play_cue_refuses_during_measurement(caplog) -> None:

    wl = wake_loop_for_tests(cues=_RefusingCues())
    assert (await wl.measurement_hold.pause_response())["result"] == "ok"

    with caplog.at_level(logging.INFO, logger="jasper.voice_daemon"):
        result = await wl.play_cue("cant_connect")

    assert result == "measurement_active"
    assert event_fields(caplog, "cue.skipped") == {
        "reason": "measurement_active",
        "slug": "cant_connect",
    }
    await wl.measurement_hold.resume()


async def test_play_cue_plays_normally_when_not_measuring() -> None:

    played: list[str] = []

    class _Cues:
        async def play(self, slug: str) -> bool:
            played.append(slug)
            return True

    wl = wake_loop_for_tests(cues=_Cues())

    assert await wl.play_cue("cant_connect") == "ok"
    assert played == ["cant_connect"]


@pytest.mark.parametrize("output_busy", [False, True])
async def test_play_supervisor_cue_refuses_during_measurement(
    caplog, output_busy: bool,
) -> None:

    wl = wake_loop_for_tests(cues=_RefusingCues())
    if output_busy:
        assert await wl._output_gate.begin_if_idle("admin") is not None
    assert (await wl.measurement_hold.pause_response())["result"] == "ok"

    with caplog.at_level(logging.INFO, logger="jasper.voice_daemon"):
        result = await wl.play_supervisor_cue("cant_connect")

    assert result == "measurement_active"
    if not output_busy:
        assert event_fields(caplog, "cue.skipped")["reason"] == "measurement_active"
    await wl.measurement_hold.resume()


@pytest.mark.parametrize(
    "entry, busy_gate, reason",
    [
        ("admitted", "measurement", "measurement_active"),
        ("admitted", "output", "busy"),
        ("owned", "output", "output_active"),
    ],
)
async def test_a_refused_cue_reaches_the_managers_health_record(
    tmp_path, entry: str, busy_gate: str, reason: str,
) -> None:
    """A gate refusal IS the deafness /state.cues exists to show, so it has
    to reach the manager the daemon publishes — the journal line alone
    leaves the snapshot reading healthy."""
    cues = AudioCueManager(
        sounds_dir=str(tmp_path), hostname="jts.local", voice="Aoede",
    )
    wl = wake_loop_for_tests(cues=cues)
    if busy_gate == "measurement":
        assert (await wl.measurement_hold.pause_response())["result"] == "ok"
    else:
        assert await wl._output_gate.begin_if_idle("admin") is not None

    if entry == "admitted":
        assert await wl.play_cue("cant_connect") == reason
    else:
        assert await wl._play_cue("cant_connect") is False

    snap = cues.snapshot()
    assert snap["last"]["outcome"] == "skipped"
    assert snap["last"]["reason"] == reason
    assert snap["last"]["slug"] == "cant_connect"
    # The manager was never asked to play, so nothing else was recorded.
    assert snap["counts"] == {
        "delivered": 0, "fallback": 0, "stale": 0, "skipped": 1, "failed": 0,
    }
    if busy_gate == "measurement":
        await wl.measurement_hold.resume()


async def test_play_supervisor_cue_plays_normally_when_not_measuring() -> None:

    played: list[str] = []

    class _Cues:
        async def play(self, slug: str) -> bool:
            played.append(slug)
            return True

    wl = wake_loop_for_tests(cues=_Cues())

    assert await wl.play_supervisor_cue("cant_connect") == "ok"
    assert played == ["cant_connect"]


async def test_announce_timer_suppressed_during_measurement(caplog) -> None:

    class _Cues:
        async def prerender_text(self, _text: str) -> bool:
            return True

        async def speak_text(self, _text: str, _should_play=None) -> None:
            raise AssertionError(
                "timer must not speak during a measurement window"
            )

    wl = wake_loop_for_tests(cues=_Cues())
    assert (await wl.measurement_hold.pause_response())["result"] == "ok"

    with caplog.at_level(logging.INFO, logger="jasper.voice_daemon"):
        await wl.announce_timer(_timer())

    assert event_fields(caplog, "dynamic_text.skipped") == {
        "reason": "measurement_active",
    }
    await wl.measurement_hold.resume()


async def test_announce_timer_speaks_normally_when_not_measuring() -> None:

    spoken: list[str] = []

    async def _play(text: str) -> bool:
        spoken.append(text)
        return True

    wl = wake_loop_for_tests()
    wl._play_dynamic_text = _play

    await wl.announce_timer(_timer())

    assert spoken == ["Your pasta timer is up."]


async def test_prerender_race_cannot_admit_timer_after_pause() -> None:
    """A timer past its early check is still stopped at atomic admission."""

    prerender_started = asyncio.Event()
    finish_prerender = asyncio.Event()
    spoke: list[str] = []

    class _Cues:
        async def prerender_text(self, _text: str) -> bool:
            prerender_started.set()
            await finish_prerender.wait()
            return True

        async def speak_text(self, text: str, _should_play=None) -> None:
            spoke.append(text)

    wl = wake_loop_for_tests(cues=_Cues())
    announce = asyncio.create_task(wl.announce_timer(_timer()))
    await asyncio.wait_for(prerender_started.wait(), timeout=1.0)

    assert (await wl.measurement_hold.pause_response())["result"] == "ok"
    finish_prerender.set()
    await asyncio.wait_for(announce, timeout=1.0)

    assert spoke == []
    assert wl._output_gate.admission_paused
    await wl.measurement_hold.resume()


async def test_measurement_pause_blocks_mute_click_admission() -> None:

    writes: list[bytes] = []
    wl = wake_loop_for_tests()

    async def write_segment(pcm, **_kwargs):
        writes.append(pcm)

    wl._tts.write_segment = write_segment
    assert (await wl.measurement_hold.pause_response())["result"] == "ok"

    await wl._play_mute_click(going_on=True)

    assert writes == []
    await wl.measurement_hold.resume()


class _RecordingTts(TtsPlayout):
    """The production emission seam (write_segment/set_emission_admission)
    over a recording transport — never opens the fan-in socket or wire-width
    resolver, so it skips TtsPlayout.__init__ and sets only the two
    attributes that seam reads."""

    def __init__(self) -> None:
        self._emission_admission = None
        self._emission_refusal_logged = False
        self.segments: list[bytes] = []

    async def _write_segment(self, pcm: bytes, on_first_write=None, **_kwargs) -> bool:
        self.segments.append(pcm)
        if on_first_write is not None:
            await on_first_write()
        return True

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

    monkeypatch.setattr(
        "jasper.voice.measurement_hold.MEASUREMENT_INFLIGHT_DRAIN_SEC", 0.0,
    )
    tts = _RecordingTts()
    wl = wake_loop_for_tests(tts=tts)
    await wl._begin_turn_output_episode()

    assert (await wl.measurement_hold.pause_response())["result"] == "ok"

    with caplog.at_level(logging.INFO, logger="jasper.audio_io"):
        await wl._play_listening_chirp(going_on=True)
        await wl._tts.write_segment(b"\x00\x00", segment_kind="assistant")

    assert tts.segments == []
    assert event_fields(caplog, "tts_write.refused")["reason"] == "measurement_active"
    await wl.measurement_hold.resume()


async def test_emission_proceeds_once_the_window_closes() -> None:

    tts = _RecordingTts()
    wl = wake_loop_for_tests(tts=tts)

    assert (await wl.measurement_hold.pause_response())["result"] == "ok"
    await wl.measurement_hold.resume()
    await wl._play_listening_chirp(going_on=True)

    assert tts.segments == [wl._assistant_output._chirp_on_pcm]


async def test_measurement_between_frames_discards_old_input_and_resets_history():
    wl = wake_loop_for_tests()
    old = np.zeros(1280, dtype=np.int16)
    fresh = np.ones(1280, dtype=np.int16)
    captured_at = time.monotonic()
    wl._pre_roll.append(old)
    wl._capture_ring_on.append(old)
    wl._acquire_buffer.append(old, captured_at)
    detector = wl._legs["on"].detector
    detector.reset = Mock()
    wl._handle_wake_frame = AsyncMock()

    async def frames():
        mic.last_frame = InputFrame(old, captured_at)
        yield old
        mic.last_frame = InputFrame(fresh, time.monotonic())
        yield fresh

    mic = SimpleNamespace(frames=frames, last_frame=None)
    wl._mic = wl._legs["on"].mic = mic
    assert (await wl.measurement_hold.pause_response())["result"] == "ok"
    await wl.measurement_hold.resume()
    assert not wl._pre_roll
    assert not wl._capture_ring_on
    assert not wl._acquire_buffer
    await wl.run()
    assert len(wl._pre_roll) == 1
    assert wl._pre_roll[0] is fresh
    wl._handle_wake_frame.assert_awaited_once_with(fresh, leg="on")
    detector.reset.assert_called_once_with()
