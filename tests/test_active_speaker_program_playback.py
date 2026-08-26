# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Program playback orchestration.

Exercises play_program with a fake aplay boundary: the session volume assertion
and the fresh re-admission both gate playback, and the writer lock is held
across the play so no other DSP writer can replace the measurement graph
mid-capture.

The graph load/restore bracket this used to own moved to
``crossover_v2.session_graph.MeasurementSessionGraph`` (wave 6b) — its tests,
including the restore-failure pins, live in
``tests/test_crossover_v2_session_graph.py``.
"""
from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest

from jasper.active_speaker.program_admission import ProgramAdmission, ProgramAdmissionRefusal
from jasper.active_speaker.program_playback import (
    ProgramPlaybackRefused,
    play_program,
)
from jasper.active_speaker.session_volume_plan import SessionVolumePlanError
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.playback import PlaybackResult
from jasper.audio_measurement.program import RoleBand, build_measure_program


def _program():
    roles = [
        RoleBand("woofer", 0, FrequencyBand(500.0, 1600.0)),
        RoleBand("tweeter", 1, FrequencyBand(1600.0, 10_000.0)),
    ]
    return build_measure_program(
        {"woofer": -6.0, "tweeter": -6.0}, roles, downstream_gain_db=-65.0
    )


def _admission(program, *, allowed=True):
    return ProgramAdmission(
        program_id=program.program_id,
        phase=program.phase,
        session_volume_db=-65.0,
        segments=(),
        channels=(),
        refusals=() if allowed else (ProgramAdmissionRefusal.CHANNEL_PEAK_OVER_CAP,),
    )


class FakePlan:
    def __init__(self, *, ready=True):
        self._ready = ready
        self.measurement_volume_db = -65.0

    def assert_ready(self):
        if not self._ready:
            raise SessionVolumePlanError("not ready")


class Boundary:
    """Records the lock/playback sequence for one play_program run."""

    def __init__(self, *, play_ok=True):
        self.play_ok = play_ok
        self.order: list = []

    async def play_wav(self):
        self.order.append("play")
        if not self.play_ok:
            raise RuntimeError("aplay failed")
        return PlaybackResult(
            wav_path=Path("prog.wav"), alsa_device="correction_substream", returncode=0
        )

    @contextlib.asynccontextmanager
    async def writer_lock(self):
        self.order.append("lock")
        try:
            yield
        finally:
            self.order.append("unlock")


def _run(program, boundary, plan, *, admission=None):
    admission = admission if admission is not None else _admission(program)

    async def readmit():
        boundary.order.append("readmit")
        return admission

    return asyncio.run(
        play_program(
            program,
            session_volume_plan=plan,
            readmit=readmit,
            play_wav=boundary.play_wav,
            writer_lock=boundary.writer_lock,
        )
    )


def test_happy_path_readmits_then_plays_under_the_lock():
    program = _program()
    boundary = Boundary()
    result = _run(program, boundary, FakePlan())
    assert boundary.order == ["readmit", "lock", "play", "unlock"]
    assert result.playback.returncode == 0
    assert result.admission.allowed


def test_the_lock_is_released_when_playback_raises():
    """The writer lock is what keeps another DSP writer off the graph during a
    capture, so a failed play must still hand it back."""
    program = _program()
    boundary = Boundary(play_ok=False)
    with pytest.raises(RuntimeError, match="aplay failed"):
        _run(program, boundary, FakePlan())
    assert boundary.order[-1] == "unlock"


def test_refused_readmission_never_takes_the_lock():
    program = _program()
    boundary = Boundary()
    with pytest.raises(ProgramPlaybackRefused):
        _run(program, boundary, FakePlan(), admission=_admission(program, allowed=False))
    # No lock taken, nothing played.
    assert boundary.order == ["readmit"]


def test_session_volume_not_ready_blocks_before_readmit():
    program = _program()
    boundary = Boundary()
    with pytest.raises(SessionVolumePlanError):
        _run(program, boundary, FakePlan(ready=False))
    assert boundary.order == []  # assert_ready runs first, before readmit
