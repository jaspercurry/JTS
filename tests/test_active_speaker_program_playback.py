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
import hashlib
from pathlib import Path

import pytest

from jasper.active_speaker.program_admission import ProgramAdmission, ProgramAdmissionRefusal
from jasper.active_speaker.program_playback import (
    ProgramGraphRestoreError,
    ProgramPlaybackError,
    ProgramPlaybackRefused,
    play_program,
    verified_program_aplay,
)
from jasper.active_speaker.session_volume_plan import SessionVolumePlanError
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.evidence_identity import ArtifactIdentity
from jasper.audio_measurement.playback import PlaybackResult
from jasper.audio_measurement.program import (
    RoleBand,
    build_measure_program,
    write_program_wav,
)

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

    def record_entry(path):
        boundary.order.append(("record", path))

    def clear_entry():
        boundary.order.append("clear")

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
            record_entry_anchor=record_entry,
            clear_entry_anchor=clear_entry,
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
        ("record", ENTRY_PATH),
        ("load", "PROGRAM_YAML"),
        "play",
        ("restore", ENTRY_PATH),
        "clear",
        "unlock",
    ]


def test_restore_runs_on_playback_failure():
    program = _program()
    boundary = Boundary(play_ok=False)
    with pytest.raises(RuntimeError, match="aplay failed"):
        _run(program, boundary, FakePlan())
    # The prior graph is restored even though playback raised.
    assert ("restore", ENTRY_PATH) in boundary.order
    assert "clear" in boundary.order
    assert boundary.order[-1] == "unlock"


def test_abort_during_playback_restores_and_clears_exact_entry_anchor():
    async def scenario():
        program = _program()
        boundary = Boundary()
        started = asyncio.Event()

        async def blocked_play():
            boundary.order.append("play")
            started.set()
            await asyncio.Future()

        boundary.play_wav = blocked_play

        async def readmit():
            boundary.order.append("readmit")
            return _admission(program)

        def record_entry(path):
            boundary.order.append(("record", path))

        def clear_entry():
            boundary.order.append("clear")

        task = asyncio.create_task(
            play_program(
                program,
                program_graph_yaml="PROGRAM_YAML",
                session_volume_plan=FakePlan(),
                readmit=readmit,
                read_current_config_path=boundary.read_current_config_path,
                load_program_graph=boundary.load_program_graph,
                restore_graph=boundary.restore_graph,
                play_wav=boundary.play_wav,
                writer_lock=boundary.writer_lock,
                record_entry_anchor=record_entry,
                clear_entry_anchor=clear_entry,
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return boundary.order

    order = asyncio.run(scenario())
    assert ("restore", ENTRY_PATH) in order
    assert "clear" in order
    assert order[-1] == "unlock"


def test_restore_runs_on_load_failure():
    program = _program()
    boundary = Boundary(load_ok=False)
    with pytest.raises(RuntimeError, match="rejected the program graph"):
        _run(program, boundary, FakePlan())
    assert ("restore", ENTRY_PATH) in boundary.order
    assert "clear" in boundary.order


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
    assert "clear" not in boundary.order
    # N3: the end marker must say restore_failed, never a false "completed".
    end_lines = [
        r.getMessage()
        for r in caplog.records
        if "active_speaker.program_playback" in r.getMessage()
        and "action=end" in r.getMessage()
    ]
    assert end_lines and all("result=restore_failed" in line for line in end_lines)
    assert not any("result=completed" in line for line in end_lines)


def test_verified_program_aplay_uses_stereo_correction_substream_contract(
    tmp_path, monkeypatch
):
    import jasper.active_speaker.program_playback as playback_module

    path = tmp_path / "program.wav"
    write_program_wav(path, _program())
    raw = path.read_bytes()
    artifact = ArtifactIdentity(
        bundle_kind="test",
        bundle_id="test",
        relative_path="program.wav",
        sha256=hashlib.sha256(raw).hexdigest(),
        byte_size=len(raw),
    )
    observed = {}

    async def fake_play(source, *, alsa_device, timeout_s):
        observed.update(
            channels=source.channels,
            sample_rate_hz=source.sample_rate_hz,
            alsa_device=alsa_device,
            timeout_s=timeout_s,
        )
        return PlaybackResult(
            wav_path=path,
            alsa_device=alsa_device,
            returncode=0,
        )

    monkeypatch.setattr(playback_module, "play_verified_wav", fake_play)
    result = asyncio.run(
        verified_program_aplay(tmp_path, artifact, timeout_s=3.0)
    )

    assert result.returncode == 0
    assert observed == {
        "channels": 2,
        "sample_rate_hz": 48000,
        "alsa_device": "correction_substream",
        "timeout_s": 3.0,
    }
