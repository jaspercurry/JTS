# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Program playback orchestration (Wave 2 deliverable D).

Exercises play_program with a fake aplay/DSP boundary: config staged -> program
played -> prior graph restored, restore still runs on playback failure, fresh
re-admission gates playback, and the session-volume assertion gates the run.
"""
from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest

from jasper.active_speaker.program_admission import ProgramAdmission, ProgramAdmissionRefusal
from jasper.active_speaker.program_playback import (
    ProgramGraphRestoreError,
    ProgramPlaybackError,
    ProgramPlaybackRefused,
    play_program,
)
from jasper.active_speaker.session_volume_plan import SessionVolumePlanError
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.playback import PlaybackResult
from jasper.audio_measurement.program import RoleBand, build_measure_program

ENTRY_PATH = "/etc/camilladsp/active_speaker_baseline.yml"


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
    """Records the staging/playback/restore sequence for one play_program run."""

    def __init__(self, *, entry_path=ENTRY_PATH, load_ok=True, play_ok=True, restore_ok=True):
        self.entry_path = entry_path
        self.load_ok = load_ok
        self.play_ok = play_ok
        self.restore_ok = restore_ok
        self.order: list = []

    async def read_current_config_path(self):
        return self.entry_path

    async def load_program_graph(self, yaml_text):
        self.order.append(("load", yaml_text))
        if not self.load_ok:
            raise RuntimeError("camilla rejected the program graph")
        return True

    async def play_wav(self):
        self.order.append("play")
        if not self.play_ok:
            raise RuntimeError("aplay failed")
        return PlaybackResult(
            wav_path=Path("prog.wav"), alsa_device="correction_substream", returncode=0
        )

    async def restore_graph(self, path):
        self.order.append(("restore", path))
        return self.restore_ok

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
            program_graph_yaml="PROGRAM_YAML",
            session_volume_plan=plan,
            readmit=readmit,
            read_current_config_path=boundary.read_current_config_path,
            load_program_graph=boundary.load_program_graph,
            restore_graph=boundary.restore_graph,
            play_wav=boundary.play_wav,
            writer_lock=boundary.writer_lock,
        )
    )


def test_happy_path_stages_plays_then_restores():
    program = _program()
    boundary = Boundary()
    result = _run(program, boundary, FakePlan())
    assert result.entry_config_path == ENTRY_PATH
    assert result.playback.returncode == 0
    # readmit before the lock; inside the lock: load -> play -> restore -> unlock.
    assert boundary.order == [
        "readmit",
        "lock",
        ("load", "PROGRAM_YAML"),
        "play",
        ("restore", ENTRY_PATH),
        "unlock",
    ]


def test_restore_runs_on_playback_failure():
    program = _program()
    boundary = Boundary(play_ok=False)
    with pytest.raises(RuntimeError, match="aplay failed"):
        _run(program, boundary, FakePlan())
    # The prior graph is restored even though playback raised.
    assert ("restore", ENTRY_PATH) in boundary.order
    assert boundary.order[-1] == "unlock"


def test_restore_runs_on_load_failure():
    program = _program()
    boundary = Boundary(load_ok=False)
    with pytest.raises(RuntimeError, match="rejected the program graph"):
        _run(program, boundary, FakePlan())
    assert ("restore", ENTRY_PATH) in boundary.order


def test_refused_readmission_never_stages():
    program = _program()
    boundary = Boundary()
    with pytest.raises(ProgramPlaybackRefused):
        _run(program, boundary, FakePlan(), admission=_admission(program, allowed=False))
    # No lock taken, nothing staged.
    assert boundary.order == ["readmit"]


def test_session_volume_not_ready_blocks_before_readmit():
    program = _program()
    boundary = Boundary()
    with pytest.raises(SessionVolumePlanError):
        _run(program, boundary, FakePlan(ready=False))
    assert boundary.order == []  # assert_ready runs first, before readmit


def test_missing_entry_config_refuses_before_loading():
    program = _program()
    boundary = Boundary(entry_path=None)
    with pytest.raises(ProgramPlaybackError, match="no current DSP config"):
        _run(program, boundary, FakePlan())
    # Lock taken, but nothing loaded or played.
    assert "load" not in [e[0] if isinstance(e, tuple) else e for e in boundary.order]


def test_restore_failure_after_play_raises(caplog):
    program = _program()
    boundary = Boundary(restore_ok=False)
    with caplog.at_level("INFO"):
        with pytest.raises(ProgramGraphRestoreError):
            _run(program, boundary, FakePlan())
    # Play still happened and restore was attempted.
    assert "play" in boundary.order
    assert ("restore", ENTRY_PATH) in boundary.order
    # N3: the end marker must say restore_failed, never a false "completed".
    end_lines = [
        r.getMessage()
        for r in caplog.records
        if "active_speaker.program_playback" in r.getMessage()
        and "action=end" in r.getMessage()
    ]
    assert end_lines and all("result=restore_failed" in line for line in end_lines)
    assert not any("result=completed" in line for line in end_lines)
