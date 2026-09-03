# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor aec_probe domain.

The active probe drives real audio hardware, so its exclusive-run lock and
audio-isolation lifecycle are non-negotiable: every body here is preserved
verbatim from the pre-split test file.
"""

import asyncio
import json
import os
import subprocess
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from jasper.cli import doctor


@pytest.fixture(autouse=True)
def _probe_lock_in_test_tmp(monkeypatch, tmp_path):
    monkeypatch.setattr(
        doctor.aec_probe,
        "_PROBE_LOCK_PATH",
        str(tmp_path / "doctor-aec-probe.lock"),
    )


def _rms_log_line(ref: int, mic: int, aec: int, attn_db: float) -> str:
    """Synthesize one bridge `rms over` log line in the journal `--output=cat`
    format the parser sees."""
    return (
        f"2026-05-16 17:00:00,000 aec-bridge INFO "
        f"rms over 5.0s: ref={ref} mic={mic} aec={aec} → "
        f"attenuation={attn_db:.1f} dB (frames=1 ref_q=0 mic_q=0 "
        f"ref_clip=0.00% out_clip=0.00%)"
    )


def test_active_aec_probe_is_owned_by_dedicated_module(monkeypatch):
    monkeypatch.setattr(
        doctor.aec_probe,
        "_run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="inactive\n"),
    )

    results = doctor.probe_aec_ref_path()

    assert doctor.probe_aec_ref_path is doctor.aec_probe.probe_aec_ref_path
    assert [(result.name, result.status, result.reason) for result in results] == [
        (
            "probe — bridge running", "fail",
            doctor.aec_probe.REASON_PROBE_BRIDGE_NOT_RUNNING,
        )
    ]


def test_aec_probe_process_lock_is_fail_fast_and_reusable(tmp_path):
    path = str(tmp_path / "probe.lock")

    with doctor.aec_probe._probe_lock(path) as fd:
        assert not os.get_inheritable(fd)
        with pytest.raises(doctor.aec_probe._ProbeLockBusy):
            with doctor.aec_probe._probe_lock(path):
                pytest.fail("a second process lock must not enter")

    # Close/process death releases flock; the stable inode is deliberately
    # retained and can be acquired again without an unlink race.
    assert Path(path).exists()
    with doctor.aec_probe._probe_lock(path):
        pass


def test_aec_probe_process_lock_excludes_a_separate_process(tmp_path):
    path = str(tmp_path / "probe.lock")
    contender = (
        "import errno,fcntl,os,sys; "
        "fd=os.open(sys.argv[1],os.O_RDWR|os.O_CREAT,0o600); "
        "\ntry: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)"
        "\nexcept OSError as exc: sys.exit(23 if exc.errno in "
        "(errno.EACCES,errno.EAGAIN) else 24)"
        "\nelse: sys.exit(0)"
    )

    with doctor.aec_probe._probe_lock(path):
        held = subprocess.run(
            [sys.executable, "-c", contender, path],
            check=False,
        )
    released = subprocess.run(
        [sys.executable, "-c", contender, path],
        check=False,
    )

    assert held.returncode == 23
    assert released.returncode == 0


def test_second_aec_probe_cannot_reach_precheck_wave_or_aplay(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(
        doctor.aec_probe,
        "_run",
        lambda cmd, **_kwargs: calls.append(cmd),
    )
    monkeypatch.setattr(
        doctor.aec_probe,
        "run_correction_play",
        lambda wav_path, **_kwargs: calls.append(["aplay", str(wav_path)]),
    )

    with doctor.aec_probe._probe_lock():
        results = doctor.probe_aec_ref_path()

    assert [(result.name, result.status) for result in results] == [
        ("probe — exclusive run", "fail")
    ]
    assert results[0].reason == doctor.aec_probe.REASON_PROBE_LOCK_BUSY
    assert "already active" in results[0].detail
    assert "No test tone was played" in results[0].detail
    assert calls == []


def test_aec_probe_reports_unavailable_process_lock(monkeypatch, tmp_path):
    monkeypatch.setattr(
        doctor.aec_probe,
        "_PROBE_LOCK_PATH",
        str(tmp_path / "missing" / "probe.lock"),
    )

    results = doctor.probe_aec_ref_path()

    assert results[0].name == "probe — exclusive run"
    assert results[0].status == "fail"
    assert results[0].reason == doctor.aec_probe.REASON_PROBE_LOCK_ERROR
    assert "/run/jasper permissions" in results[0].detail


def _active_probe_run_recorder():
    """One ledger over BOTH probe spawn seams.

    Since P6c-i the sine spawn rides jasper.audio_measurement.
    correction_lane.run_correction_play (which does a REAL subprocess.run),
    while systemctl/journalctl still go through the module's `_run`. The
    not-reached tests must fake both: a regression that reached the play
    stage with only `_run` faked would spawn a real aplay instead of
    tripping the ledger assert. Both fakes record into one `calls` list so
    the existing `cmd[0] == "aplay"` asserts keep guarding the real seam.
    """
    calls = []

    def _run(cmd, **_kwargs):
        calls.append(cmd)
        if cmd[:2] == ["systemctl", "is-active"]:
            return SimpleNamespace(stdout="active\n", stderr="", returncode=0)
        raise AssertionError(f"probe continued unexpectedly: {cmd}")

    def _play(wav_path, **_kwargs):
        calls.append(["aplay", str(wav_path)])
        raise AssertionError(f"probe played unexpectedly: {wav_path}")

    return calls, _run, _play


@pytest.mark.parametrize(
    "error",
    [
        doctor.aec_probe.control.ControlError("connection refused"),
        json.JSONDecodeError("invalid JSON", "not-json", 0),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
    ],
    ids=["control-unreachable", "malformed-json", "invalid-utf8"],
)
def test_active_aec_probe_fails_closed_when_state_unavailable(monkeypatch, error):
    calls, fake_run, fake_play = _active_probe_run_recorder()

    def raise_state_error(**_kwargs):
        raise error

    monkeypatch.setattr(doctor.aec_probe, "_run", fake_run)
    monkeypatch.setattr(doctor.aec_probe, "run_correction_play", fake_play)
    monkeypatch.setattr(
        doctor.aec_probe.control,
        "get_state",
        raise_state_error,
    )

    results = doctor.probe_aec_ref_path()

    assert [result.status for result in results] == ["ok", "fail"]
    assert results[-1].reason == doctor.aec_probe.REASON_PROBE_CONTROL_STATE_UNAVAILABLE
    assert "idleness could not be established" in results[-1].detail
    assert "no test tone was played" in results[-1].detail
    assert not any(call and call[0] == "aplay" for call in calls)


@pytest.mark.parametrize(
    "state",
    [
        {},
        {"active_source": None},
        {"active_source": 1},
        {"active_source": ["idle"]},
    ],
    ids=["missing", "null", "integer", "list"],
)
def test_active_aec_probe_fails_closed_for_untrusted_active_source(
    monkeypatch, state
):
    calls, fake_run, fake_play = _active_probe_run_recorder()
    monkeypatch.setattr(doctor.aec_probe, "_run", fake_run)
    monkeypatch.setattr(doctor.aec_probe, "run_correction_play", fake_play)
    monkeypatch.setattr(
        doctor.aec_probe.control, "get_state", lambda **_kwargs: state
    )

    results = doctor.probe_aec_ref_path()

    assert [result.status for result in results] == ["ok", "fail"]
    assert results[-1].reason == doctor.aec_probe.REASON_PROBE_ACTIVE_SOURCE_UNKNOWN
    assert "trustworthy active_source" in results[-1].detail
    assert "no test tone was played" in results[-1].detail
    assert not any(call and call[0] == "aplay" for call in calls)


@pytest.mark.parametrize(
    ("active_source", "detail"),
    [
        ("spotify", "active_source='spotify'"),
        ("voice", "active_source='voice'"),
        ("airplay", "active_source='airplay'"),
        ("usbsink", "active_source='usbsink'"),
        ("bluetooth", "active_source='bluetooth'"),
    ],
)
def test_active_aec_probe_refuses_known_active_playback(
    monkeypatch, active_source, detail
):
    """A non-idle `active_source` still refuses, and plays nothing (#2585).

    This is the precheck that survived the deletion of the `/proc/asound`
    fan-in-lane layer, so it carries the whole "tell the operator to stop the
    source" job now. It is TRANSPORT-AGNOSTIC on purpose — `active_source` is
    mux's effective winner, not a lane read — which is why every source id is
    exercised rather than only the two the old parametrization named. The
    `not any(... "aplay" ...)` assertion is the load-bearing half: refusing
    with a message while still playing a tone would be the failure this
    guards.
    """
    calls, fake_run, fake_play = _active_probe_run_recorder()
    monkeypatch.setattr(doctor.aec_probe, "_run", fake_run)
    monkeypatch.setattr(doctor.aec_probe, "run_correction_play", fake_play)
    monkeypatch.setattr(
        doctor.aec_probe.control,
        "get_state",
        lambda **_kwargs: {"active_source": active_source},
    )

    results = doctor.probe_aec_ref_path()

    assert [result.status for result in results] == ["ok", "fail"]
    assert results[-1].reason == doctor.aec_probe.REASON_PROBE_ACTIVE_SOURCE_BUSY
    assert detail in results[-1].detail
    assert "Stop the active source and re-run" in results[-1].detail
    assert not any(call and call[0] == "aplay" for call in calls)


def test_active_aec_probe_has_no_proc_asound_lane_precheck(monkeypatch):
    """The retired `/proc/asound` fan-in-lane layer stays retired (#2585).

    An absence guard, because the deleted check was PERMANENTLY INERT on a
    ring-armed box: re-adding it would read as protection while protecting
    nothing there, and the property is held by the measurement window's mux
    gate plus the `active_source` precheck above. Named-symbol absence rather
    than a grep so a comment describing the retirement cannot satisfy it.
    """
    assert not hasattr(doctor.aec_probe, "_loopback_playback_active")
    # Positive control: the module DOES still re-export the sibling helpers it
    # uses, so the assertion above is discriminating rather than vacuous.
    assert hasattr(doctor.aec_probe, "_run")


def test_active_aec_probe_trustworthy_idle_reaches_aplay(monkeypatch, tmp_path):
    calls = []
    isolation_open = False

    @asynccontextmanager
    async def fake_measurement_window(**kwargs):
        nonlocal isolation_open
        assert kwargs == {
            "gate_owner": "doctor-aec-probe",
            "require_voice_pause": True,
        }
        with pytest.raises(doctor.aec_probe._ProbeLockBusy):
            with doctor.aec_probe._probe_lock():
                pytest.fail("process lock must cover measurement cleanup")
        isolation_open = True
        try:
            yield
        finally:
            isolation_open = False

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        if cmd[:2] == ["systemctl", "is-active"]:
            return SimpleNamespace(stdout="active\n", stderr="", returncode=0)
        if cmd and cmd[0] == "journalctl":
            return SimpleNamespace(
                stdout=_rms_log_line(ref=300, mic=400, aec=80, attn_db=-14.0),
                stderr="",
                returncode=0,
            )
        raise AssertionError(f"unexpected command: {cmd}")

    def fake_play(wav_path, **_kwargs):
        # P6c-i: the sine spawn rides the shared correction-lane helper.
        calls.append(["aplay", str(wav_path)])
        assert isolation_open
        assert Path(doctor.aec_probe._PROBE_SINE_PATH).exists()
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(doctor.aec_probe, "_run", fake_run)
    monkeypatch.setattr(doctor.aec_probe, "run_correction_play", fake_play)
    monkeypatch.setattr(
        doctor.aec_probe, "measurement_window", fake_measurement_window
    )
    monkeypatch.setattr(
        doctor.aec_probe.control,
        "get_state",
        lambda **_kwargs: {"active_source": "idle"},
    )
    monkeypatch.setattr(doctor.aec_probe, "_PROBE_SINE_DURATION_S", 0.0)
    monkeypatch.setattr(
        doctor.aec_probe, "_PROBE_SINE_PATH", str(tmp_path / "probe.wav")
    )
    monkeypatch.setattr(doctor.aec_probe.time, "sleep", lambda _seconds: None)

    results = doctor.probe_aec_ref_path()

    assert [result.status for result in results] == ["ok", "ok", "ok", "ok"]
    assert [result.reason for result in results] == [
        doctor.aec_probe.REASON_PROBE_BRIDGE_RUNNING,
        doctor.aec_probe.REASON_PROBE_RENDERERS_IDLE,
        doctor.aec_probe.REASON_PROBE_APLAY_OK,
        doctor.aec_probe.REASON_PROBE_REF_HEALTHY,
    ]
    assert any(call and call[0] == "aplay" for call in calls)
    assert isolation_open is False


def test_active_aec_probe_releases_isolation_after_aplay_failure(
    monkeypatch, tmp_path
):
    events: list[str] = []

    @asynccontextmanager
    async def fake_measurement_window(**_kwargs):
        events.append("isolation-enter")
        try:
            yield
        finally:
            events.append("isolation-exit")

    def fake_run(cmd, **_kwargs):
        if cmd[:2] == ["systemctl", "is-active"]:
            return SimpleNamespace(stdout="active\n", stderr="", returncode=0)
        raise AssertionError(f"unexpected command: {cmd}")

    def fake_play(_wav_path, **_kwargs):
        # P6c-i: the sine spawn rides the shared correction-lane helper.
        events.append("aplay-failed")
        return SimpleNamespace(stdout="", stderr="device busy", returncode=1)

    monkeypatch.setattr(doctor.aec_probe, "_run", fake_run)
    monkeypatch.setattr(doctor.aec_probe, "run_correction_play", fake_play)
    monkeypatch.setattr(
        doctor.aec_probe.control,
        "get_state",
        lambda **_kwargs: {"active_source": "idle"},
    )
    monkeypatch.setattr(
        doctor.aec_probe, "measurement_window", fake_measurement_window
    )
    monkeypatch.setattr(doctor.aec_probe, "_PROBE_SINE_DURATION_S", 0.0)
    monkeypatch.setattr(
        doctor.aec_probe, "_PROBE_SINE_PATH", str(tmp_path / "probe.wav")
    )

    results = doctor.probe_aec_ref_path()

    assert results[-1].name == "probe — aplay sine"
    assert results[-1].status == "fail"
    assert results[-1].reason == doctor.aec_probe.REASON_PROBE_APLAY_FAILED
    assert events == ["isolation-enter", "aplay-failed", "isolation-exit"]


def test_active_aec_probe_never_generates_or_plays_without_isolation(
    monkeypatch, tmp_path
):
    calls, fake_run, fake_play = _active_probe_run_recorder()

    @asynccontextmanager
    async def unavailable_window(**kwargs):
        assert kwargs["gate_owner"] == "doctor-aec-probe"
        raise doctor.aec_probe.MeasurementWindowError("mux busy")
        yield  # pragma: no cover

    sine_path = tmp_path / "must-not-exist.wav"
    monkeypatch.setattr(doctor.aec_probe, "_run", fake_run)
    monkeypatch.setattr(doctor.aec_probe, "run_correction_play", fake_play)
    monkeypatch.setattr(
        doctor.aec_probe.control,
        "get_state",
        lambda **_kwargs: {"active_source": "idle"},
    )
    monkeypatch.setattr(doctor.aec_probe, "measurement_window", unavailable_window)
    monkeypatch.setattr(doctor.aec_probe, "_PROBE_SINE_PATH", str(sine_path))

    results = doctor.probe_aec_ref_path()

    assert [result.status for result in results] == ["ok", "ok", "fail"]
    assert results[-1].name == "probe — audio isolation"
    assert results[-1].reason == doctor.aec_probe.REASON_PROBE_ISOLATION_UNAVAILABLE
    assert "no test tone was played" in results[-1].detail
    assert not sine_path.exists()
    assert not any(call and call[0] == "aplay" for call in calls)


async def test_active_aec_probe_keeps_isolation_until_cancelled_body_stops(
    monkeypatch,
):
    events: list[str] = []
    entered_body = asyncio.Event()
    unblock_body = threading.Event()
    loop = asyncio.get_running_loop()

    @asynccontextmanager
    async def fake_measurement_window(**_kwargs):
        events.append("isolation-enter")
        try:
            yield
        finally:
            events.append("isolation-exit")

    def blocking_body():
        loop.call_soon_threadsafe(entered_body.set)
        assert unblock_body.wait(timeout=1.0)
        events.append("body-stopped")
        return []

    monkeypatch.setattr(
        doctor.aec_probe, "measurement_window", fake_measurement_window
    )
    monkeypatch.setattr(doctor.aec_probe, "_play_and_assess_probe", blocking_body)

    task = asyncio.create_task(doctor.aec_probe._run_isolated_probe())
    await asyncio.wait_for(entered_body.wait(), timeout=1.0)
    task.cancel()
    await asyncio.sleep(0)
    assert events == ["isolation-enter"]

    task.cancel()
    await asyncio.sleep(0)
    assert events == ["isolation-enter"]

    unblock_body.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert events == ["isolation-enter", "body-stopped", "isolation-exit"]


def test_active_aec_probe_reports_exit_cleanup_failure_after_tone(monkeypatch):
    @asynccontextmanager
    async def release_fails(**_kwargs):
        yield
        raise doctor.aec_probe.MeasurementWindowError("mux release stuck")

    monkeypatch.setattr(
        doctor.aec_probe,
        "_run",
        lambda cmd, **_kwargs: SimpleNamespace(
            stdout="active\n" if cmd[:2] == ["systemctl", "is-active"] else "",
            stderr="",
            returncode=0,
        ),
    )
    monkeypatch.setattr(
        doctor.aec_probe.control,
        "get_state",
        lambda **_kwargs: {"active_source": "idle"},
    )
    monkeypatch.setattr(doctor.aec_probe, "measurement_window", release_fails)
    monkeypatch.setattr(
        doctor.aec_probe,
        "_play_and_assess_probe",
        lambda: [doctor.CheckResult("probe — aplay sine", "ok", "tone completed")],
    )

    results = doctor.probe_aec_ref_path()

    assert [result.name for result in results] == [
        "probe — bridge running",
        "probe — renderers idle",
        "probe — aplay sine",
        "probe — audio isolation cleanup",
    ]
    assert results[-1].status == "fail"
    assert results[-1].reason == doctor.aec_probe.REASON_PROBE_ISOLATION_CLEANUP_FAILED
    assert "probe body completed" in results[-1].detail
    assert "playback outcome is shown above" in results[-1].detail
    assert "test tone ran" not in results[-1].detail.lower()
    assert "no test tone was played" not in results[-1].detail.lower()


def test_active_aec_probe_preserves_generate_failure_on_cleanup_failure(
    monkeypatch,
):
    @asynccontextmanager
    async def release_fails(**_kwargs):
        yield
        raise doctor.aec_probe.MeasurementWindowError("mux release stuck")

    monkeypatch.setattr(
        doctor.aec_probe,
        "_run",
        lambda cmd, **_kwargs: SimpleNamespace(
            stdout="active\n" if cmd[:2] == ["systemctl", "is-active"] else "",
            stderr="",
            returncode=0,
        ),
    )
    monkeypatch.setattr(
        doctor.aec_probe.control,
        "get_state",
        lambda **_kwargs: {"active_source": "idle"},
    )
    monkeypatch.setattr(doctor.aec_probe, "measurement_window", release_fails)
    monkeypatch.setattr(
        doctor.aec_probe,
        "_play_and_assess_probe",
        lambda: [
            doctor.CheckResult(
                "probe — generate sine",
                "fail",
                "could not write probe file",
            )
        ],
    )

    results = doctor.probe_aec_ref_path()

    assert [result.name for result in results] == [
        "probe — bridge running",
        "probe — renderers idle",
        "probe — generate sine",
        "probe — audio isolation cleanup",
    ]
    assert results[-2].status == "fail"
    assert "could not write probe file" in results[-2].detail
    assert results[-1].reason == doctor.aec_probe.REASON_PROBE_ISOLATION_CLEANUP_FAILED
    assert "probe body completed" in results[-1].detail
    assert "test tone ran" not in results[-1].detail.lower()
