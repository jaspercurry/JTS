# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from jasper import audio_validation
from jasper.cli import audio_hw_validate
from tests.test_audio_validation import (
    NOW,
    _active_chip_inputs,
    _bridge_sample,
    _chip_readback,
    _outputd_sample,
    _outputd_stability_inputs,
)


def test_run_audio_hardware_validation_refuses_inactive_without_force(monkeypatch):
    inputs = _active_chip_inputs()
    mode_env = dict(inputs["mode_env"])
    mode_env["JASPER_WAKE_LEG_CHIP_AEC"] = "0"
    system_env = dict(inputs["system_env"])
    system_env["JASPER_AEC_CHIP_AEC_ENABLED"] = "0"
    system_env["JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS"] = "disclosed_stale"

    monkeypatch.setattr(audio_hw_validate, "_read_mode_env", lambda: mode_env)
    monkeypatch.setattr(audio_hw_validate, "_read_system_env", lambda: system_env)
    monkeypatch.setattr(audio_hw_validate, "_probe_xvf_mic", lambda: inputs["mic_probe"])
    monkeypatch.setattr(
        audio_hw_validate,
        "_collect_service_states",
        lambda: inputs["service_states"],
    )
    monkeypatch.setattr(
        audio_hw_validate,
        "_query_outputd_status",
        lambda _socket: inputs["outputd_status"],
    )
    monkeypatch.setattr(audio_hw_validate, "_read_bridge_stats", lambda: inputs["bridge_stats"])
    monkeypatch.setattr(
        audio_hw_validate,
        "_read_voice_wake_legs",
        lambda: inputs["voice_wake_legs"],
    )

    result = audio_hw_validate.run_audio_hardware_validation(
        report_only=True,
        now=NOW,
    )

    assert result.refused is True
    assert result.artifact is None
    assert "not the active runtime profile" in result.refusal_reason


def test_run_audio_hardware_validation_report_only_does_not_write(monkeypatch):
    inputs = _active_chip_inputs()
    wrote: list[str] = []

    monkeypatch.setattr(audio_hw_validate, "_read_mode_env", lambda: inputs["mode_env"])
    monkeypatch.setattr(audio_hw_validate, "_read_system_env", lambda: inputs["system_env"])
    monkeypatch.setattr(audio_hw_validate, "_probe_xvf_mic", lambda: inputs["mic_probe"])
    monkeypatch.setattr(
        audio_hw_validate,
        "_collect_service_states",
        lambda: inputs["service_states"],
    )
    monkeypatch.setattr(
        audio_hw_validate,
        "_query_outputd_status",
        lambda _socket: inputs["outputd_status"],
    )
    monkeypatch.setattr(audio_hw_validate, "_read_bridge_stats", lambda: inputs["bridge_stats"])
    monkeypatch.setattr(
        audio_hw_validate,
        "_read_voice_wake_legs",
        lambda: inputs["voice_wake_legs"],
    )
    monkeypatch.setattr(
        audio_hw_validate,
        "write_artifact",
        lambda *_args, **_kwargs: wrote.append("artifact"),
    )
    monkeypatch.setattr(
        audio_hw_validate,
        "write_latest_pointer",
        lambda *_args, **_kwargs: wrote.append("latest"),
    )

    result = audio_hw_validate.run_audio_hardware_validation(
        report_only=True,
        now=NOW,
    )

    assert result.refused is False
    assert result.artifact is not None
    assert result.artifact.checks["outputd_reference_health"]["status"] == "not_run"
    assert wrote == []


def test_run_audio_hardware_validation_uses_one_bounded_window(
    monkeypatch,
    tmp_path,
):
    inputs = _active_chip_inputs()
    outputd_samples = iter([
        _outputd_sample(reference_sequence=10, dac_frames_written=1000),
        _outputd_sample(reference_sequence=11, dac_frames_written=2000),
        _outputd_sample(reference_sequence=15, dac_frames_written=6000),
    ])
    bridge_samples = iter([
        _bridge_sample(frames_processed=100),
        _bridge_sample(frames_processed=110),
        _bridge_sample(frames_processed=150),
    ])
    sleeps: list[float] = []
    chip_poll_durations: list[float] = []

    monkeypatch.setattr(audio_hw_validate, "_read_mode_env", lambda: inputs["mode_env"])
    monkeypatch.setattr(audio_hw_validate, "_read_system_env", lambda: inputs["system_env"])
    monkeypatch.setattr(audio_hw_validate, "_probe_xvf_mic", lambda: inputs["mic_probe"])
    monkeypatch.setattr(
        audio_hw_validate,
        "_collect_service_states",
        lambda: inputs["service_states"],
    )
    monkeypatch.setattr(
        audio_hw_validate,
        "_query_outputd_status",
        lambda _socket: next(outputd_samples),
    )
    monkeypatch.setattr(audio_hw_validate, "_read_bridge_stats", lambda: next(bridge_samples))
    monkeypatch.setattr(
        audio_hw_validate,
        "_read_voice_wake_legs",
        lambda: inputs["voice_wake_legs"],
    )
    monkeypatch.setattr(audio_hw_validate.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(
        audio_hw_validate,
        "_read_chip_profile_parameters",
        lambda: _chip_readback(),
    )

    def poll_chip(**kwargs):
        chip_poll_durations.append(kwargs["duration_seconds"])
        return [{audio_validation.CHIP_AEC_CONVERGENCE_COMMAND: [1]}]

    monkeypatch.setattr(audio_hw_validate, "_poll_chip_convergence", poll_chip)

    result = audio_hw_validate.run_audio_hardware_validation(
        directory=tmp_path,
        duration_seconds=10,
        now=NOW,
    )

    assert result.refused is False
    assert result.path is not None
    assert sleeps == [1.0]
    assert chip_poll_durations == [9.0]
    assert result.artifact is not None
    assert result.artifact.checks["outputd_reference_health"]["status"] == "pass"


def test_poll_chip_convergence_uses_full_window_after_convergence(monkeypatch):
    reads: list[float] = []
    sleeps: list[float] = []
    now = [100.0]

    def read_xvf_parameter(command, *, timeout):
        assert command == audio_validation.CHIP_AEC_CONVERGENCE_COMMAND
        assert timeout == 5.0
        reads.append(now[0])
        return {command: [1]}

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(audio_hw_validate, "_read_xvf_parameter", read_xvf_parameter)
    monkeypatch.setattr(audio_hw_validate.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(audio_hw_validate.time, "sleep", sleep)

    polls = audio_hw_validate._poll_chip_convergence(
        duration_seconds=10,
        interval_seconds=4,
    )

    assert sleeps == [4, 4, 2]
    assert reads == [100.0, 104.0, 108.0, 110.0]
    assert polls == [
        {audio_validation.CHIP_AEC_CONVERGENCE_COMMAND: [1]},
        {audio_validation.CHIP_AEC_CONVERGENCE_COMMAND: [1]},
        {audio_validation.CHIP_AEC_CONVERGENCE_COMMAND: [1]},
        {audio_validation.CHIP_AEC_CONVERGENCE_COMMAND: [1]},
    ]


def test_run_outputd_stability_profile_does_not_probe_chip_or_voice(
    monkeypatch,
    tmp_path,
):
    inputs = _outputd_stability_inputs()
    outputd_samples = iter([
        _outputd_sample(reference_sequence=10, dac_frames_written=1000),
        _outputd_sample(reference_sequence=17, dac_frames_written=8000),
    ])
    sleeps: list[float] = []

    monkeypatch.setattr(audio_hw_validate, "_read_system_env", lambda: inputs["system_env"])
    monkeypatch.setattr(
        audio_hw_validate,
        "_collect_service_states",
        lambda: inputs["service_states"],
    )
    monkeypatch.setattr(
        audio_hw_validate,
        "_query_outputd_status",
        lambda _socket: next(outputd_samples),
    )
    monkeypatch.setattr(audio_hw_validate.time, "sleep", lambda seconds: sleeps.append(seconds))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("chip-AEC probe path should not run for outputd stability")

    monkeypatch.setattr(audio_hw_validate, "_read_mode_env", forbidden)
    monkeypatch.setattr(audio_hw_validate, "_probe_xvf_mic", forbidden)
    monkeypatch.setattr(audio_hw_validate, "_read_bridge_stats", forbidden)
    monkeypatch.setattr(audio_hw_validate, "_read_voice_wake_legs", forbidden)
    monkeypatch.setattr(audio_hw_validate, "_read_chip_profile_parameters", forbidden)
    monkeypatch.setattr(audio_hw_validate, "_poll_chip_convergence", forbidden)

    result = audio_hw_validate.run_audio_hardware_validation(
        profile=audio_validation.DAC8X_OUTPUTD_STABILITY_PROFILE,
        directory=tmp_path,
        duration_seconds=10,
        now=NOW,
    )

    assert result.refused is False
    assert result.path is not None
    assert sleeps == [10]
    assert result.artifact is not None
    assert result.artifact.status == "pass"
    assert result.artifact.profile == audio_validation.DAC8X_OUTPUTD_STABILITY_PROFILE
