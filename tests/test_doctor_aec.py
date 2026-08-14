# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor aec domain."""

import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from jasper.audio_profile_state import MicProbe
from jasper.cli import doctor


@pytest.fixture(autouse=True)
def _probe_lock_in_test_tmp(monkeypatch, tmp_path):
    monkeypatch.setattr(
        doctor.aec_probe,
        "_PROBE_LOCK_PATH",
        str(tmp_path / "doctor-aec-probe.lock"),
    )


# --------------------------------------------- AEC bridge output assessment


def _rms_log_line(ref: int, mic: int, aec: int, attn_db: float) -> str:
    """Synthesize one bridge `rms over` log line in the journal `--output=cat`
    format the parser sees. Helper for the _assess_aec_bridge_output tests
    below."""
    return (
        f"2026-05-16 17:00:00,000 aec-bridge INFO "
        f"rms over 5.0s: ref={ref} mic={mic} aec={aec} → "
        f"attenuation={attn_db:.1f} dB (frames=1 ref_q=0 mic_q=0 "
        f"ref_clip=0.00% out_clip=0.00%)"
    )


def _bridge_reference_stats(
    source: str,
    *,
    now: float = 1_000.0,
    age_sec: float = 1.0,
    endpoint: str = "127.0.0.1:9891",
) -> dict:
    identity = {"ref_source": source}
    if source == "outputd_udp":
        identity["outputd_ref_udp"] = endpoint
    return {
        "updated_epoch_sec": now - age_sec,
        "active_capture_plan": {"mic_reference_identity": identity},
    }


def _outputd_reference_status(
    *,
    target: object = "127.0.0.1:9891",
    active: bool = True,
    error_count: int = 0,
) -> dict:
    return {
        "reference_outputs": {
            "udp_target": target,
            "udp_active": active,
            "udp_error_count": error_count,
        },
    }


def test_active_aec_probe_is_owned_by_dedicated_module(monkeypatch):
    monkeypatch.setattr(
        doctor.aec_probe,
        "_run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="inactive\n"),
    )

    results = doctor.probe_aec_ref_path()

    assert doctor.probe_aec_ref_path is doctor.aec_probe.probe_aec_ref_path
    assert [(result.name, result.status) for result in results] == [
        ("probe — bridge running", "fail")
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
    assert "trustworthy active_source" in results[-1].detail
    assert "no test tone was played" in results[-1].detail
    assert not any(call and call[0] == "aplay" for call in calls)


@pytest.mark.parametrize(
    ("active_source", "loopback_active", "detail"),
    [
        ("spotify", False, "active_source='spotify'"),
        ("voice", False, "active_source='voice'"),
        ("idle", True, "fan-in input lane is currently open"),
    ],
)
def test_active_aec_probe_refuses_known_active_playback(
    monkeypatch, active_source, loopback_active, detail
):
    calls, fake_run, fake_play = _active_probe_run_recorder()
    monkeypatch.setattr(doctor.aec_probe, "_run", fake_run)
    monkeypatch.setattr(doctor.aec_probe, "run_correction_play", fake_play)
    monkeypatch.setattr(
        doctor.aec_probe.control,
        "get_state",
        lambda **_kwargs: {"active_source": active_source},
    )
    monkeypatch.setattr(
        doctor.aec_probe, "_loopback_playback_active", lambda: loopback_active
    )

    results = doctor.probe_aec_ref_path()

    assert [result.status for result in results] == ["ok", "fail"]
    assert detail in results[-1].detail
    assert not any(call and call[0] == "aplay" for call in calls)


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
    monkeypatch.setattr(doctor.aec_probe, "_loopback_playback_active", lambda: False)
    monkeypatch.setattr(doctor.aec_probe, "_PROBE_SINE_DURATION_S", 0.0)
    monkeypatch.setattr(
        doctor.aec_probe, "_PROBE_SINE_PATH", str(tmp_path / "probe.wav")
    )
    monkeypatch.setattr(doctor.aec_probe.time, "sleep", lambda _seconds: None)

    results = doctor.probe_aec_ref_path()

    assert [result.status for result in results] == ["ok", "ok", "ok", "ok"]
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
    monkeypatch.setattr(doctor.aec_probe, "_loopback_playback_active", lambda: False)
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
    monkeypatch.setattr(doctor.aec_probe, "_loopback_playback_active", lambda: False)
    monkeypatch.setattr(doctor.aec_probe, "measurement_window", unavailable_window)
    monkeypatch.setattr(doctor.aec_probe, "_PROBE_SINE_PATH", str(sine_path))

    results = doctor.probe_aec_ref_path()

    assert [result.status for result in results] == ["ok", "ok", "fail"]
    assert results[-1].name == "probe — audio isolation"
    assert "no test tone was played" in results[-1].detail
    assert not sine_path.exists()
    assert not any(call and call[0] == "aplay" for call in calls)


@pytest.mark.asyncio
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
    monkeypatch.setattr(doctor.aec_probe, "_loopback_playback_active", lambda: False)
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
    monkeypatch.setattr(doctor.aec_probe, "_loopback_playback_active", lambda: False)
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
    assert "probe body completed" in results[-1].detail
    assert "test tone ran" not in results[-1].detail.lower()


def test_assess_aec_output_empty_journal_is_ok():
    """No rms lines = bridge probably just restarted in the assessment
    window. Not a failure, just nothing to evaluate."""
    r = doctor._assess_aec_bridge_output("")
    assert r.status == "ok"
    assert "no recent rms windows" in r.detail.lower()


def test_assess_aec_output_idle_returns_ok():
    """Mic and ref both quiet — speaker has been idle, no music has
    played. Doctor must NOT flag this as a degradation."""
    lines = [_rms_log_line(ref=0, mic=200, aec=30, attn_db=-16.5) for _ in range(10)]
    r = doctor._assess_aec_bridge_output("\n".join(lines))
    assert r.status == "ok"
    assert "no music activity" in r.detail.lower()


def test_assess_aec_output_silent_ref_with_no_healthy_window_fails():
    """The PR #75 dsnoop rate-lock signature: mic shows music acoustically
    throughout, ref delivers silence throughout, ZERO windows prove the
    ref chain ever worked in this period. The check MUST fail — this is
    the regression we exist to catch."""
    lines = [_rms_log_line(ref=0, mic=2500, aec=2400, attn_db=-0.4) for _ in range(8)]
    r = doctor._assess_aec_bridge_output("\n".join(lines))
    assert r.status == "fail"
    assert "reference path is delivering silence" in r.detail
    assert "Runtime reference provenance is unavailable" in r.detail
    assert "pcm.jasper_capture" not in r.detail
    assert "/etc/asound.conf" not in r.detail


def test_assess_aec_output_outputd_remediation_uses_runtime_status():
    lines = [_rms_log_line(ref=0, mic=2500, aec=2400, attn_db=-0.4) for _ in range(8)]

    result = doctor._assess_aec_bridge_output(
        "\n".join(lines),
        bridge_stats=_bridge_reference_stats("outputd_udp"),
        outputd_status=_outputd_reference_status(error_count=3),
        now=1_000.0,
    )

    assert result.status == "fail"
    assert "source=outputd_udp at 127.0.0.1:9891" in result.detail
    assert "reference_outputs.udp_target='127.0.0.1:9891'" in result.detail
    assert "udp_active=True" in result.detail
    assert "udp_error_count=3" in result.detail
    assert "jasper-aec-bridge" in result.detail
    assert "pcm.jasper_capture" not in result.detail
    assert "/etc/asound.conf" not in result.detail
    assert "jasper_ref" not in result.detail


def test_assess_aec_output_outputd_endpoint_mismatch_reconciles_both_hops():
    lines = [_rms_log_line(ref=0, mic=2500, aec=2400, attn_db=-0.4) for _ in range(8)]

    result = doctor._assess_aec_bridge_output(
        "\n".join(lines),
        bridge_stats=_bridge_reference_stats(
            "outputd_udp",
            endpoint="127.0.0.1:9891",
        ),
        outputd_status=_outputd_reference_status(
            target="127.0.0.1:9999",
            active=False,
            error_count=4,
        ),
        now=1_000.0,
    )

    assert result.status == "fail"
    assert "publisher target and bridge receiver do not match" in result.detail
    assert "jasper-aec-reconcile" in result.detail
    assert "restart jasper-outputd and jasper-aec-bridge" in result.detail
    assert "127.0.0.1:9999" in result.detail


@pytest.mark.parametrize(
    "outputd_status",
    [
        {
            "reference_outputs": {
                "udp_active": True,
                "udp_error_count": 0,
            },
        },
        _outputd_reference_status(target=9891),
        _outputd_reference_status(target="not-an-endpoint"),
    ],
    ids=["missing", "non-string", "malformed"],
)
def test_assess_aec_output_unusable_outputd_target_is_comparison_neutral(
    outputd_status,
):
    lines = [_rms_log_line(ref=0, mic=2500, aec=2400, attn_db=-0.4) for _ in range(8)]

    result = doctor._assess_aec_bridge_output(
        "\n".join(lines),
        bridge_stats=_bridge_reference_stats("outputd_udp"),
        outputd_status=outputd_status,
        now=1_000.0,
    )

    assert result.status == "fail"
    assert "source=outputd_udp" in result.detail
    assert "no comparable UDP target" in result.detail
    assert "cannot declare an endpoint match or mismatch" in result.detail
    assert "publisher target and bridge receiver do not match" not in result.detail
    assert "jasper-aec-reconcile" in result.detail
    assert "pcm.jasper_capture" not in result.detail
    assert "/etc/asound.conf" not in result.detail


def test_assess_aec_output_retired_alsa_provenance_gets_no_asound_advice():
    """U4 / P7-1: the bridge cannot read `pcm.jasper_ref` any more.

    A stats file still claiming `source=alsa` therefore names a producer
    that does not exist, so doctor must fall through to the generic
    unknown-provenance remediation instead of sending the operator to
    /etc/asound.conf for a hop nothing reads.
    """
    lines = [_rms_log_line(ref=0, mic=2500, aec=2400, attn_db=-0.4) for _ in range(8)]

    result = doctor._assess_aec_bridge_output(
        "\n".join(lines),
        bridge_stats=_bridge_reference_stats("alsa"),
        now=1_000.0,
    )

    assert result.status == "fail"
    assert "cannot safely name the failed hop" in result.detail
    assert "source=alsa" not in result.detail
    assert "/etc/asound.conf" not in result.detail
    assert "pcm.jasper_capture" not in result.detail
    assert "jasper_ref" not in result.detail


@pytest.mark.parametrize(
    "stats",
    [
        _bridge_reference_stats("future_transport"),
        {
            "updated_epoch_sec": 999.0,
            "active_capture_plan": {"mic_reference_identity": "malformed"},
        },
    ],
)
def test_assess_aec_output_unknown_or_malformed_provenance_is_neutral(stats):
    lines = [_rms_log_line(ref=0, mic=2500, aec=2400, attn_db=-0.4) for _ in range(8)]

    result = doctor._assess_aec_bridge_output(
        "\n".join(lines),
        bridge_stats=stats,
        outputd_status=_outputd_reference_status(),
        now=1_000.0,
    )

    assert result.status == "fail"
    assert "cannot safely name the failed hop" in result.detail
    assert "source=outputd_udp" not in result.detail
    assert "source=alsa" not in result.detail
    assert "pcm.jasper_capture" not in result.detail
    assert "/etc/asound.conf" not in result.detail


def test_assess_aec_output_stale_reference_stats_are_ignored():
    lines = [_rms_log_line(ref=0, mic=2500, aec=2400, attn_db=-0.4) for _ in range(8)]

    result = doctor._assess_aec_bridge_output(
        "\n".join(lines),
        bridge_stats=_bridge_reference_stats(
            "outputd_udp",
            age_sec=31.0,
        ),
        outputd_status=_outputd_reference_status(),
        now=1_000.0,
    )

    assert result.status == "fail"
    assert "unavailable, malformed, stale, or unknown" in result.detail
    assert "source=outputd_udp" not in result.detail
    assert "pcm.jasper_capture" not in result.detail
    assert "/etc/asound.conf" not in result.detail


def test_reference_provenance_rejects_json_valid_oversized_timestamp():
    huge_json_integer = "1" + ("0" * 4_000)
    stats = json.loads(
        '{"updated_epoch_sec":'
        + huge_json_integer
        + ',"active_capture_plan":{"mic_reference_identity":'
        '{"ref_source":"outputd_udp",'
        '"outputd_ref_udp":"127.0.0.1:9891"}}}'
    )

    detail = doctor.aec._aec_reference_failure_remediation(
        bridge_stats=stats,
        outputd_status=_outputd_reference_status(),
        now=1_000.0,
    )

    assert "cannot safely name the failed hop" in detail
    assert "source=outputd_udp" not in detail


def test_reference_provenance_rejects_future_timestamp():
    stats = _bridge_reference_stats(
        "outputd_udp",
        now=1_000.0,
        age_sec=-1.0,
    )

    assert doctor.aec._bridge_reference_provenance(stats, 1_000.0) is None


def test_check_aec_output_health_uses_live_outputd_status_on_failure(monkeypatch):
    journal = "\n".join(
        _rms_log_line(ref=0, mic=2500, aec=2400, attn_db=-0.4) for _ in range(8)
    )

    def fake_run(command, **_kwargs):
        if command[:2] == ["systemctl", "is-active"]:
            return SimpleNamespace(stdout="active\n", stderr="", returncode=0)
        return SimpleNamespace(stdout=journal, stderr="", returncode=0)

    monkeypatch.setattr(doctor.aec, "_parked_as_bonded_follower", lambda: False)
    monkeypatch.setattr(doctor.aec, "_run", fake_run)
    monkeypatch.setattr(doctor.aec, "_loopback_playback_active", lambda: True)
    monkeypatch.setattr(
        doctor.aec,
        "_read_bridge_stats_snapshot",
        lambda: _bridge_reference_stats("outputd_udp", now=1_000.0),
    )
    monkeypatch.setattr(doctor.aec.time, "time", lambda: 1_000.0)
    monkeypatch.setattr(
        doctor.aec,
        "_read_outputd_status_for_aec_reference",
        lambda: _outputd_reference_status(error_count=7),
    )

    result = doctor.check_aec_bridge_output_health()

    assert result.status == "fail"
    assert "reference_outputs.udp_target='127.0.0.1:9891'" in result.detail
    assert "udp_error_count=7" in result.detail


def test_check_aec_output_health_skips_outputd_status_when_reference_is_healthy(
    monkeypatch,
):
    journal = "\n".join(
        _rms_log_line(ref=1200, mic=2400, aec=150, attn_db=-24.1) for _ in range(8)
    )

    def fake_run(command, **_kwargs):
        if command[:2] == ["systemctl", "is-active"]:
            return SimpleNamespace(stdout="active\n", stderr="", returncode=0)
        return SimpleNamespace(stdout=journal, stderr="", returncode=0)

    monkeypatch.setattr(doctor.aec, "_parked_as_bonded_follower", lambda: False)
    monkeypatch.setattr(doctor.aec, "_run", fake_run)
    monkeypatch.setattr(doctor.aec, "_loopback_playback_active", lambda: True)
    monkeypatch.setattr(
        doctor.aec,
        "_read_bridge_stats_snapshot",
        lambda: _bridge_reference_stats("outputd_udp", now=1_000.0),
    )
    monkeypatch.setattr(doctor.aec.time, "time", lambda: 1_000.0)
    monkeypatch.setattr(
        doctor.aec,
        "_read_outputd_status_for_aec_reference",
        lambda: pytest.fail("healthy passive check must not probe outputd STATUS"),
    )

    result = doctor.check_aec_bridge_output_health()

    assert result.status == "ok"
    assert "real AEC work" in result.detail


def test_assess_aec_output_silent_ref_downgrades_when_loopback_closed():
    """Same mic-loud + ref-silent shape as the rate-lock fail, but no
    renderer is writing the loopback. A silent ref is then expected
    rather than suspicious, and the mic-loud bursts are most likely
    room voice or ambient noise. Downgrade to OK with the diagnosis so
    a pure-voice session doesn't show as a degraded AEC bridge. The
    message must also disclose the gate's coverage limit in FULL: it sees
    only the snd-aloop renderer lanes, so BOTH a USB Audio Input stream
    and any ring-armed renderer lane (U3/P6) are invisible to it. Naming
    only USB would leave an armed box's operator reading a clean OK with
    an explanation that does not cover their box."""
    lines = [_rms_log_line(ref=0, mic=2500, aec=2400, attn_db=-0.4) for _ in range(8)]
    r = doctor._assess_aec_bridge_output(
        "\n".join(lines),
        music_chain_active=False,
    )
    assert r.status == "ok"
    assert "loopback playback is closed" in r.detail
    assert "USB Audio Input and any ring-armed renderer lane are invisible" in r.detail
    # Counterpart: when music chain IS active, same input still fails —
    # the guard only relaxes the FAIL when we have positive evidence
    # the loopback is idle, not on uncertainty.
    r_active = doctor._assess_aec_bridge_output(
        "\n".join(lines),
        music_chain_active=True,
    )
    assert r_active.status == "fail"


def test_assess_aec_output_silent_ref_with_healthy_window_is_ok():
    """The 2026-05-16 false-positive: loud room voice / ambient noise
    push silent_ref over threshold, but at least one window in the
    assessment period has ref signal (proving the chain works). The
    check must NOT fail — silent-ref windows have benign explanations
    when the ref path is demonstrably alive."""
    lines = [
        # 5 mic-loud + ref-silent windows from non-reference loud sound.
        _rms_log_line(ref=0, mic=2200, aec=2100, attn_db=-0.4),
        _rms_log_line(ref=0, mic=2400, aec=2300, attn_db=-0.4),
        _rms_log_line(ref=0, mic=2600, aec=2500, attn_db=-0.3),
        _rms_log_line(ref=0, mic=2100, aec=2050, attn_db=-0.2),
        _rms_log_line(ref=0, mic=2300, aec=2250, attn_db=-0.2),
        # 2 windows where music played and ref captured it correctly
        _rms_log_line(ref=800, mic=2400, aec=200, attn_db=-21.6),
        _rms_log_line(ref=1100, mic=2800, aec=180, attn_db=-23.8),
    ]
    r = doctor._assess_aec_bridge_output("\n".join(lines))
    assert r.status == "ok"
    assert "likely room voice or ambient noise" in r.detail
    assert "ref path proven healthy" in r.detail


# The retired pre-outputd TTS bypass (``pcm.jasper_out`` → the raw physical
# card, removed from the ALSA template by issue #2240). Assistant TTS now rides
# the fan-in → CamillaDSP → outputd path and lands in the AEC reference like
# any other program audio, so naming it as the reason a window is ref-silent is
# a wrong diagnosis. Matched at a word boundary so it never collides with the
# ``jasper-outputd`` unit name or the ``JASPER_OUTPUTD_`` env prefix.
RETIRED_TTS_BYPASS_RE = re.compile(r"\bjasper_out\b")


def test_doctor_never_explains_ref_silence_with_the_retired_tts_bypass():
    """No module under `jasper/cli/doctor/` may name ``jasper_out`` — the
    pre-outputd dmix that genuinely bypassed the reference. It was retired in
    #2240; citing it in a diagnosis sends an operator chasing a path that no
    longer exists, and hides the real explanation (sound the speaker never
    played)."""
    # Positive/negative control: a typo'd pattern would otherwise pass
    # forever by matching nothing.
    assert RETIRED_TTS_BYPASS_RE.search("pcm.jasper_out")
    assert not RETIRED_TTS_BYPASS_RE.search("jasper-outputd")
    assert not RETIRED_TTS_BYPASS_RE.search("jasper_outputd")
    assert not RETIRED_TTS_BYPASS_RE.search("JASPER_OUTPUTD_TTS_SOCKET")
    doctor_dir = Path(doctor.aec.__file__).resolve().parent
    modules = sorted(doctor_dir.glob("*.py"))
    assert modules, "no doctor modules found to scan"
    offenders = {
        path.name
        for path in modules
        if RETIRED_TTS_BYPASS_RE.search(path.read_text(encoding="utf-8"))
    }
    assert not offenders, (
        "retired 'jasper_out' TTS bypass must not appear in doctor "
        f"diagnostics; found in: {sorted(offenders)}"
    )


def test_assess_aec_output_healthy_aec_work_is_ok():
    """Music playing through the loopback, ref strong, attenuation
    meaningful — the bridge is doing its job. ok with a summary."""
    lines = [
        _rms_log_line(ref=1200, mic=2400, aec=150, attn_db=-24.1) for _ in range(8)
    ]
    r = doctor._assess_aec_bridge_output("\n".join(lines))
    assert r.status == "ok"
    assert "real AEC work" in r.detail


def test_assess_aec_output_drift_warnings_warn():
    """High count of `drained N stale ref frames (drift)` warnings
    indicates ref/mic clock skew or rate mismatch. Warn, don't fail."""
    drift_line = (
        "2026-05-16 17:00:00,000 aec-bridge WARNING drained 7 stale ref frames (drift)"
    )
    # Threshold is 30 in 90 s; 40 is comfortably over.
    journal = "\n".join([drift_line] * 40)
    r = doctor._assess_aec_bridge_output(journal)
    assert r.status == "warn"
    assert "ref-drift warnings" in r.detail


def test_assess_aec_output_single_healthy_window_suffices():
    """Boundary: exactly one healthy_ref window flips the silent-ref
    pattern from fail to ok. Documents the design choice — if the ref
    chain proved itself once in the window, we trust it."""
    lines = [_rms_log_line(ref=0, mic=2500, aec=2400, attn_db=-0.4) for _ in range(7)]
    lines.append(_rms_log_line(ref=300, mic=400, aec=80, attn_db=-14.0))
    r = doctor._assess_aec_bridge_output("\n".join(lines))
    assert r.status == "ok"


def test_assess_aec_output_silent_ref_below_alarm_surfaces_in_summary():
    """When silent_ref_count is 1-4 (non-zero but below the fail
    threshold of 5), the OK summary appends a `silent-ref=N` note so
    intermittent ref glitches are visible before they tip into a real
    outage. Per PR #124 upstream — preserved in the refactor."""
    lines = [
        _rms_log_line(ref=1200, mic=2400, aec=150, attn_db=-24.1) for _ in range(6)
    ]
    # 3 mic-loud + ref-silent windows: above 0 but below the 5-count alarm.
    lines += [_rms_log_line(ref=0, mic=2200, aec=2100, attn_db=-0.4) for _ in range(3)]
    r = doctor._assess_aec_bridge_output("\n".join(lines))
    assert r.status == "ok"
    assert "silent-ref=3" in r.detail
    assert "below alarm" in r.detail


def test_loopback_playback_active_reads_proc_status(tmp_path):
    """Helper must report True for any non-closed subdev and False when
    every subdev is closed. Verifies the first-line strip-and-compare
    against the actual /proc/asound status file format (single word
    `closed` vs `state: RUNNING\\n…`)."""
    fake_root = tmp_path / "asound" / "Loopback" / "pcm0p"
    fake_root.mkdir(parents=True)
    sub_paths = []
    for sub in range(4):
        d = fake_root / f"sub{sub}"
        d.mkdir()
        status = d / "status"
        status.write_text("closed\n")
        sub_paths.append(str(status))

    with patch("glob.glob", return_value=sub_paths):
        # All closed → inactive.
        assert doctor._loopback_playback_active() is False
        # Flip sub2 to RUNNING → active.
        (fake_root / "sub2" / "status").write_text(
            "state: RUNNING\nowner_pid   : 12345\n"
        )
        assert doctor._loopback_playback_active() is True

    # No status files at all (e.g., snd-aloop not loaded) → inactive,
    # never raises.
    with patch("glob.glob", return_value=[]):
        assert doctor._loopback_playback_active() is False


def _reference_input_stats(
    *,
    now_monotonic: float = 1_000.0,
    schema_version: int = 4,
    snapshot_age_sec: float = 0.5,
    process_age_sec: float = 60.0,
    updated_epoch_sec: float = 50_000.0,
    started_epoch_sec: float = 40_000.0,
    source: str = "outputd_udp",
    endpoint: str = "127.0.0.1:9891",
    frames_enqueued: int = 100,
    last_frame_age_ms: float | None = 100.0,
    ref_starved_frames: int = 0,
) -> dict:
    return {
        "schema_version": schema_version,
        "updated_epoch_sec": updated_epoch_sec,
        "started_epoch_sec": started_epoch_sec,
        "active_capture_plan": {
            "mic_reference_identity": {
                "ref_source": source,
                "outputd_ref_udp": endpoint,
            }
        },
        "reference_input": {
            "source": source,
            "endpoint": endpoint,
            "frames_enqueued": frames_enqueued,
            "last_frame_age_ms": last_frame_age_ms,
            "snapshot_monotonic_ms": (
                now_monotonic - snapshot_age_sec
            ) * 1000,
            "process_age_ms": max(
                0.0,
                process_age_sec - snapshot_age_sec,
            ) * 1000,
        },
        "counters": {"ref_starved_frames": ref_starved_frames},
    }


def _active_outputd_reference_status(
    *,
    target: str = "127.0.0.1:9891",
    active: bool = True,
    errors: int = 0,
) -> dict:
    return {
        "reference_outputs": {
            "udp_target": target,
            "udp_active": active,
            "udp_error_count": errors,
        }
    }


def _assess_reference_stats(stats: dict, *, now_monotonic: float = 1_000.0):
    return doctor.aec._assess_aec_reference_input_from_stats(
        stats,
        now_monotonic,
        configured_source="outputd_udp",
        expected_endpoint="127.0.0.1:9891",
        outputd_status=_active_outputd_reference_status(),
    )


def test_assess_reference_input_recent_receiver_is_ok():
    assessed = _assess_reference_stats(_reference_input_stats())

    assert assessed is not None
    result, startup_grace = assessed
    assert result.status == "ok"
    assert startup_grace is False
    assert "receiver current" in result.detail


def test_assess_reference_input_no_frame_after_startup_grace_fails():
    assessed = _assess_reference_stats(
        _reference_input_stats(
            frames_enqueued=0,
            last_frame_age_ms=None,
        )
    )

    assert assessed is not None
    result, startup_grace = assessed
    assert result.status == "fail"
    assert startup_grace is False
    assert "zero complete 20 ms reference frames" in result.detail


@pytest.mark.parametrize(
    ("source", "endpoint"),
    [
        ("alsa", "jasper_ref"),
        ("outputd_udp", "127.0.0.1:9999"),
    ],
)
def test_assess_reference_input_runtime_identity_mismatch_fails(
    source,
    endpoint,
):
    assessed = _assess_reference_stats(
        _reference_input_stats(source=source, endpoint=endpoint)
    )

    assert assessed is not None
    result, _startup_grace = assessed
    assert result.status == "fail"
    assert "does not match configured outputd UDP input" in result.detail


def test_assess_reference_input_formerly_nonzero_but_frozen_fails():
    assessed = _assess_reference_stats(
        _reference_input_stats(
            frames_enqueued=55_000,
            last_frame_age_ms=5_100,
        )
    )

    assert assessed is not None
    result, _startup_grace = assessed
    assert result.status == "fail"
    assert "receiver is stale" in result.detail
    assert "carried-forward AEC frame" in result.detail


@pytest.mark.parametrize(
    "stats",
    [
        {},
        _reference_input_stats(schema_version=3),
        _reference_input_stats(schema_version=5),
    ],
    ids=["missing-schema", "old-schema", "future-schema"],
)
def test_assess_reference_input_undeclared_schema_preserves_fallback(stats):
    assert _assess_reference_stats(stats) is None


@pytest.mark.parametrize(
    ("stats", "expected_detail"),
    [
        (
            {
                **_reference_input_stats(),
                "reference_input": {"source": "outputd_udp"},
            },
            "missing required field",
        ),
        (
            _reference_input_stats(snapshot_age_sec=31.0),
            "stats writer has not advanced",
        ),
        (
            _reference_input_stats(snapshot_age_sec=-0.1),
            "is in the future",
        ),
    ],
    ids=["malformed", "writer-stale", "future-monotonic"],
)
def test_assess_reference_input_declared_v4_fails_closed(stats, expected_detail):
    assessed = _assess_reference_stats(stats)

    assert assessed is not None
    result, startup_grace = assessed
    assert result.status == "fail"
    assert startup_grace is False
    assert expected_detail in result.detail


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("snapshot_monotonic_ms", float("nan")),
        ("snapshot_monotonic_ms", float("inf")),
        ("snapshot_monotonic_ms", -1),
        ("snapshot_monotonic_ms", 10**1000),
        ("process_age_ms", float("nan")),
        ("process_age_ms", -1),
        ("last_frame_age_ms", float("inf")),
        ("last_frame_age_ms", -1),
        ("last_frame_age_ms", 10**1000),
        ("frames_enqueued", 1 << 64),
    ],
)
def test_assess_reference_input_rejects_untrusted_numeric_fields(field, value):
    stats = _reference_input_stats()
    stats["reference_input"][field] = value

    assessed = _assess_reference_stats(stats)

    assert assessed is not None
    result, _startup_grace = assessed
    assert result.status == "fail"
    assert "untrustworthy" in result.detail


@pytest.mark.parametrize(
    ("updated_epoch_sec", "started_epoch_sec"),
    [
        (10**12, 10**12 - 60),
        (-10**12, -10**12 - 60),
    ],
    ids=["wall-clock-forward", "wall-clock-backward"],
)
def test_assess_reference_input_ignores_wall_clock_jumps(
    updated_epoch_sec,
    started_epoch_sec,
):
    fresh = _assess_reference_stats(
        _reference_input_stats(
            updated_epoch_sec=updated_epoch_sec,
            started_epoch_sec=started_epoch_sec,
            last_frame_age_ms=100,
        )
    )
    startup = _assess_reference_stats(
        _reference_input_stats(
            updated_epoch_sec=updated_epoch_sec,
            started_epoch_sec=started_epoch_sec,
            process_age_sec=9.0,
            frames_enqueued=0,
            last_frame_age_ms=None,
        )
    )
    stale = _assess_reference_stats(
        _reference_input_stats(
            updated_epoch_sec=updated_epoch_sec,
            started_epoch_sec=started_epoch_sec,
            last_frame_age_ms=9_000,
        )
    )

    assert fresh is not None and fresh[0].status == "ok" and fresh[1] is False
    assert startup is not None and startup[0].status == "ok" and startup[1] is True
    assert stale is not None and stale[0].status == "fail"


def test_assess_reference_input_young_process_gets_explicit_grace():
    assessed = _assess_reference_stats(
        _reference_input_stats(
            process_age_sec=9.9,
            frames_enqueued=0,
            last_frame_age_ms=None,
        )
    )

    assert assessed is not None
    result, startup_grace = assessed
    assert result.status == "ok"
    assert startup_grace is True
    assert "10s startup grace" in result.detail


def test_assess_reference_input_sender_active_is_not_receiver_proof():
    assessed = _assess_reference_stats(
        _reference_input_stats(last_frame_age_ms=9_000)
    )

    assert assessed is not None
    result, _startup_grace = assessed
    assert result.status == "fail"
    assert "sender active" in result.detail
    assert "send success is not receiver proof" in result.detail


@pytest.mark.parametrize(
    ("status", "error", "expected_detail"),
    [
        ({}, None, "missing reference_outputs"),
        (
            _active_outputd_reference_status(target="127.0.0.1:9999"),
            None,
            "udp_target='127.0.0.1:9999'",
        ),
        (
            _active_outputd_reference_status(active=False, errors=2),
            None,
            "reports UDP inactive",
        ),
        (None, "socket unavailable", "STATUS unavailable"),
    ],
)
def test_reference_input_failure_localizes_outputd_without_using_it_as_proof(
    status,
    error,
    expected_detail,
):
    assessed = doctor.aec._assess_aec_reference_input_from_stats(
        _reference_input_stats(last_frame_age_ms=9_000),
        1_000.0,
        configured_source="outputd_udp",
        expected_endpoint="127.0.0.1:9891",
        outputd_status=status,
        outputd_status_error=error,
    )

    assert assessed is not None
    result, _startup_grace = assessed
    assert result.status == "fail"
    assert expected_detail in result.detail


def test_assess_reference_input_historical_starvation_does_not_fail_recent_ref():
    assessed = _assess_reference_stats(
        _reference_input_stats(
            last_frame_age_ms=50,
            ref_starved_frames=1_000_000,
        )
    )

    assert assessed is not None
    result, _startup_grace = assessed
    assert result.status == "ok"


@pytest.mark.parametrize("configured_source", ["alsa", "custom"])
def test_assess_reference_input_non_udp_sources_keep_journal_policy(
    configured_source,
):
    assert (
        doctor.aec._assess_aec_reference_input_from_stats(
            _reference_input_stats(),
            1_000.0,
            configured_source=configured_source,
            expected_endpoint="jasper_ref",
        )
        is None
    )


def _install_reference_health_check_fakes(
    monkeypatch,
    tmp_path: Path,
    *,
    stats: dict | str,
    journal: str,
) -> list[list[str]]:
    stats_path = tmp_path / "aec_bridge_stats.json"
    stats_path.write_text(
        stats if isinstance(stats, str) else json.dumps(stats),
        encoding="utf-8",
    )
    monkeypatch.setenv("JASPER_AEC_REF_SOURCE", "outputd_udp")
    monkeypatch.setenv("JASPER_AEC_OUTPUTD_REF_UDP_HOST", "127.0.0.1")
    monkeypatch.setenv("JASPER_AEC_OUTPUTD_REF_UDP_PORT", "9891")
    monkeypatch.setenv("JASPER_AEC_BRIDGE_STATS_PATH", str(stats_path))
    monkeypatch.setattr(doctor.aec.time, "monotonic", lambda: 1_000.0)
    monkeypatch.setattr(doctor.aec.time, "time", lambda: 50_000.0)
    monkeypatch.setattr(doctor.aec, "_parked_as_bonded_follower", lambda: False)
    calls: list[list[str]] = []

    def fake_outputd_status():
        calls.append(["outputd-status"])
        return _active_outputd_reference_status()

    monkeypatch.setattr(
        doctor.aec,
        "_read_outputd_status_for_aec_reference",
        fake_outputd_status,
    )

    def fake_run(command, **_kwargs):
        calls.append(command)
        if command == ["systemctl", "is-active", "jasper-aec-bridge.service"]:
            return SimpleNamespace(returncode=0, stdout="active\n", stderr="")
        if command[:3] == ["journalctl", "-u", "jasper-aec-bridge.service"]:
            return SimpleNamespace(returncode=0, stdout=journal, stderr="")
        raise AssertionError(f"unexpected command: {command!r}")

    monkeypatch.setattr(doctor.aec, "_run", fake_run)
    return calls


def test_check_reference_freshness_fails_with_usb_invisible_to_loopback(
    monkeypatch,
    tmp_path: Path,
):
    calls = _install_reference_health_check_fakes(
        monkeypatch,
        tmp_path,
        stats=_reference_input_stats(last_frame_age_ms=8_000),
        journal="",
    )
    monkeypatch.setattr(doctor.aec, "_loopback_playback_active", lambda: False)

    result = doctor.aec.check_aec_bridge_output_health()

    assert result.status == "fail"
    assert "receiver is stale" in result.detail
    assert not any(command[0] == "journalctl" for command in calls)
    assert sum(command[0] == "outputd-status" for command in calls) == 1


def test_check_reference_freshness_failure_cannot_be_overridden_by_rms(
    monkeypatch,
    tmp_path: Path,
):
    healthy_rms = "\n".join(
        _rms_log_line(ref=1_200, mic=2_400, aec=150, attn_db=-24.1)
        for _ in range(8)
    )
    calls = _install_reference_health_check_fakes(
        monkeypatch,
        tmp_path,
        stats=_reference_input_stats(last_frame_age_ms=8_000),
        journal=healthy_rms,
    )

    result = doctor.aec.check_aec_bridge_output_health()

    assert result.status == "fail"
    assert "historical RMS cannot prove current receiver progress" in result.detail
    assert not any(command[0] == "journalctl" for command in calls)
    assert sum(command[0] == "outputd-status" for command in calls) == 1


@pytest.mark.parametrize("schema_version", [3, 5])
def test_check_undeclared_reference_stats_fall_back_to_journal(
    monkeypatch,
    tmp_path: Path,
    schema_version,
):
    calls = _install_reference_health_check_fakes(
        monkeypatch,
        tmp_path,
        stats=_reference_input_stats(schema_version=schema_version),
        journal="",
    )
    monkeypatch.setattr(doctor.aec, "_loopback_playback_active", lambda: False)

    result = doctor.aec.check_aec_bridge_output_health()

    assert result.status == "ok"
    assert "no recent RMS windows" in result.detail
    assert any(command[0] == "journalctl" for command in calls)
    assert not any(command[0] == "outputd-status" for command in calls)


@pytest.mark.parametrize("snapshot_age_sec", [29.0, 31.0])
def test_check_declared_v4_never_ages_from_fail_into_journal_fallback(
    monkeypatch,
    tmp_path: Path,
    snapshot_age_sec,
):
    calls = _install_reference_health_check_fakes(
        monkeypatch,
        tmp_path,
        stats=_reference_input_stats(
            snapshot_age_sec=snapshot_age_sec,
            last_frame_age_ms=100,
        ),
        journal="",
    )

    result = doctor.aec.check_aec_bridge_output_health()

    assert result.status == "fail"
    assert not any(command[0] == "journalctl" for command in calls)
    assert sum(command[0] == "outputd-status" for command in calls) == 1


def test_check_malformed_declared_v4_fails_without_row_traceback(
    monkeypatch,
    tmp_path: Path,
):
    stats = _reference_input_stats()
    del stats["reference_input"]["process_age_ms"]
    calls = _install_reference_health_check_fakes(
        monkeypatch,
        tmp_path,
        stats=stats,
        journal="",
    )

    result = doctor.aec.check_aec_bridge_output_health()

    assert result.status == "fail"
    assert "missing required field 'process_age_ms'" in result.detail
    assert not any(command[0] == "journalctl" for command in calls)
    assert sum(command[0] == "outputd-status" for command in calls) == 1


def test_check_oversized_json_integer_preserves_fallback_without_traceback(
    monkeypatch,
    tmp_path: Path,
):
    oversized = (
        '{"schema_version":4,"reference_input":'
        '{"snapshot_monotonic_ms":' + ("9" * 5_000) + "}}"
    )
    calls = _install_reference_health_check_fakes(
        monkeypatch,
        tmp_path,
        stats=oversized,
        journal="",
    )
    monkeypatch.setattr(doctor.aec, "_loopback_playback_active", lambda: False)

    result = doctor.aec.check_aec_bridge_output_health()

    assert result.status == "ok"
    assert "no recent RMS windows" in result.detail
    assert any(command[0] == "journalctl" for command in calls)
    assert not any(command[0] == "outputd-status" for command in calls)


def test_applied_reference_source_reads_the_v4_receiver_not_the_legacy_plan():
    """The applied source is `reference_input.source`, the v4 receiver field.

    Where the v4 block and the epoch-based `active_capture_plan` disagree,
    this module's shipped ruling is that v4 wins (see
    `test_check_fresh_v4_identity_overrides_contradictory_legacy_plan`), so
    the two are given contradictory values here and v4 must be the answer.
    """
    stats = _reference_input_stats()
    stats["active_capture_plan"]["mic_reference_identity"] = {
        "ref_source": "alsa",
    }

    assert doctor.aec._applied_reference_source(stats) == "outputd_udp"


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda s: s.pop("reference_input"), id="block-absent"),
        pytest.param(
            lambda s: s.update(reference_input="outputd_udp"),
            id="block-not-an-object",
        ),
        pytest.param(
            lambda s: s["reference_input"].pop("source"), id="source-absent",
        ),
        pytest.param(
            lambda s: s["reference_input"].update(source="  "),
            id="source-blank",
        ),
        pytest.param(
            lambda s: s["reference_input"].update(source=4),
            id="source-not-a-string",
        ),
    ],
)
def test_applied_reference_source_is_fail_soft(mutate):
    """Anything unreadable returns None so the caller falls back to env."""
    stats = _reference_input_stats()
    mutate(stats)

    assert doctor.aec._applied_reference_source(stats) is None


@pytest.mark.parametrize("stats", [None, {}, "not-a-dict", 4])
def test_applied_reference_source_tolerates_a_missing_snapshot(stats):
    """No snapshot (or a non-object one) is the rolling-deploy path."""
    assert doctor.aec._applied_reference_source(stats) is None


def test_stale_env_ref_source_still_runs_the_authoritative_check(
    monkeypatch,
    tmp_path: Path,
):
    """HS-N2: a stale `alsa` env must not skip the v4 freshness assessment.

    A box parked by a pre-P7-1 reconciler keeps `JASPER_AEC_REF_SOURCE=alsa`
    in /etc/jasper/jasper.env while the bridge converges to `outputd_udp`
    and publishes that in its snapshot. Gating on the env dropped exactly
    that box to the music-conditional journal fallback, which returns OK for
    a dead reference whenever no snd-aloop renderer lane is open.
    """
    calls = _install_reference_health_check_fakes(
        monkeypatch,
        tmp_path,
        stats=_reference_input_stats(last_frame_age_ms=8_000),
        journal="",
    )
    monkeypatch.setenv("JASPER_AEC_REF_SOURCE", "alsa")
    monkeypatch.setattr(doctor.aec, "_loopback_playback_active", lambda: False)

    result = doctor.aec.check_aec_bridge_output_health()

    assert result.status == "fail"
    assert "receiver is stale" in result.detail
    assert not any(command[0] == "journalctl" for command in calls)
    assert sum(command[0] == "outputd-status" for command in calls) == 1


def test_env_route_still_reaches_the_receiver_identity_fail(
    monkeypatch,
    tmp_path: Path,
):
    """The mirror case must keep its loud FAIL rather than being skipped.

    Env says `outputd_udp`, the receiver reports `alsa`. Resolving the route
    from the snapshot ALONE would close the gate and silently degrade to the
    journal; the env half of the OR keeps this reaching the assessor's
    runtime-identity FAIL.
    """
    stats = _reference_input_stats()
    stats["reference_input"]["source"] = "alsa"
    calls = _install_reference_health_check_fakes(
        monkeypatch,
        tmp_path,
        stats=stats,
        journal="",
    )

    result = doctor.aec.check_aec_bridge_output_health()

    assert result.status == "fail"
    assert "does not match configured outputd UDP input" in result.detail
    assert not any(command[0] == "journalctl" for command in calls)


def test_neither_route_outputd_keeps_the_journal_fallback(
    monkeypatch,
    tmp_path: Path,
):
    """Both ends off the outputd route → unchanged legacy journal policy.

    This is what stops the OR from becoming "always enforce": a box neither
    configured for nor running the outputd reference keeps the pre-existing
    fallback and never pays for an outputd STATUS read.
    """
    stats = _reference_input_stats()
    stats["reference_input"]["source"] = "alsa"
    calls = _install_reference_health_check_fakes(
        monkeypatch,
        tmp_path,
        stats=stats,
        journal="",
    )
    monkeypatch.setenv("JASPER_AEC_REF_SOURCE", "alsa")
    monkeypatch.setattr(doctor.aec, "_loopback_playback_active", lambda: False)

    result = doctor.aec.check_aec_bridge_output_health()

    assert result.status == "ok"
    assert "no recent RMS windows" in result.detail
    assert any(command[0] == "journalctl" for command in calls)
    assert not any(command[0] == "outputd-status" for command in calls)


def test_check_fresh_receiver_still_fails_silent_reference_content(
    monkeypatch,
    tmp_path: Path,
):
    silent_ref = "\n".join(
        _rms_log_line(ref=0, mic=2_500, aec=2_400, attn_db=-0.4)
        for _ in range(5)
    )
    calls = _install_reference_health_check_fakes(
        monkeypatch,
        tmp_path,
        stats=_reference_input_stats(last_frame_age_ms=100),
        journal=silent_ref,
    )
    monkeypatch.setattr(doctor.aec, "_loopback_playback_active", lambda: True)

    result = doctor.aec.check_aec_bridge_output_health()

    assert result.status == "fail"
    assert "reference path is delivering silence" in result.detail
    assert any(command[0] == "journalctl" for command in calls)
    assert sum(command[0] == "outputd-status" for command in calls) == 1


@pytest.mark.parametrize(
    "updated_epoch_sec",
    [10**12, -(10**12)],
    ids=["future-wall-clock", "backward-wall-clock"],
)
def test_check_fresh_v4_journal_failure_uses_monotonic_identity(
    monkeypatch,
    tmp_path: Path,
    updated_epoch_sec,
):
    silent_ref = "\n".join(
        _rms_log_line(ref=0, mic=2_500, aec=2_400, attn_db=-0.4)
        for _ in range(5)
    )
    calls = _install_reference_health_check_fakes(
        monkeypatch,
        tmp_path,
        stats=_reference_input_stats(
            updated_epoch_sec=updated_epoch_sec,
            last_frame_age_ms=100,
        ),
        journal=silent_ref,
    )
    monkeypatch.setattr(doctor.aec, "_loopback_playback_active", lambda: True)

    result = doctor.aec.check_aec_bridge_output_health()

    assert result.status == "fail"
    assert "source=outputd_udp at 127.0.0.1:9891" in result.detail
    assert "reference_outputs.udp_target='127.0.0.1:9891'" in result.detail
    assert "provenance is unavailable" not in result.detail
    assert sum(command[0] == "outputd-status" for command in calls) == 1


def test_check_fresh_v4_identity_overrides_contradictory_legacy_plan(
    monkeypatch,
    tmp_path: Path,
):
    stats = _reference_input_stats(last_frame_age_ms=100)
    stats["active_capture_plan"]["mic_reference_identity"] = {
        "ref_source": "alsa",
    }
    silent_ref = "\n".join(
        _rms_log_line(ref=0, mic=2_500, aec=2_400, attn_db=-0.4)
        for _ in range(5)
    )
    calls = _install_reference_health_check_fakes(
        monkeypatch,
        tmp_path,
        stats=stats,
        journal=silent_ref,
    )
    monkeypatch.setattr(doctor.aec, "_loopback_playback_active", lambda: True)

    result = doctor.aec.check_aec_bridge_output_health()

    assert result.status == "fail"
    assert "source=outputd_udp at 127.0.0.1:9891" in result.detail
    assert "source=alsa" not in result.detail
    assert "pcm.jasper_capture" not in result.detail
    assert sum(command[0] == "outputd-status" for command in calls) == 1


@pytest.mark.parametrize(
    "legacy_source",
    # `alsa` is the retired source (U4 / P7-1) and gets no special
    # treatment: it reads exactly like a source doctor has never heard of.
    ["alsa", "future_transport"],
    ids=["retired-alsa", "unknown"],
)
def test_check_legacy_non_outputd_fallback_skips_status(
    monkeypatch,
    tmp_path: Path,
    legacy_source,
):
    stats = _reference_input_stats(schema_version=3)
    stats["active_capture_plan"]["mic_reference_identity"] = {
        "ref_source": legacy_source,
    }
    silent_ref = "\n".join(
        _rms_log_line(ref=0, mic=2_500, aec=2_400, attn_db=-0.4)
        for _ in range(5)
    )
    calls = _install_reference_health_check_fakes(
        monkeypatch,
        tmp_path,
        stats=stats,
        journal=silent_ref,
    )
    monkeypatch.setattr(doctor.aec, "_loopback_playback_active", lambda: True)

    result = doctor.aec.check_aec_bridge_output_health()

    assert result.status == "fail"
    assert "cannot safely name the failed hop" in result.detail
    assert not any(command[0] == "outputd-status" for command in calls)


def test_check_fresh_receiver_and_one_healthy_rms_window_is_ok(
    monkeypatch,
    tmp_path: Path,
):
    journal = "\n".join(
        [
            *(
                _rms_log_line(ref=0, mic=2_500, aec=2_400, attn_db=-0.4)
                for _ in range(5)
            ),
            _rms_log_line(ref=900, mic=2_500, aec=180, attn_db=-22.8),
        ]
    )
    calls = _install_reference_health_check_fakes(
        monkeypatch,
        tmp_path,
        stats=_reference_input_stats(last_frame_age_ms=100),
        journal=journal,
    )
    monkeypatch.setattr(doctor.aec, "_loopback_playback_active", lambda: True)

    result = doctor.aec.check_aec_bridge_output_health()

    assert result.status == "ok"
    assert "ref path proven healthy" in result.detail
    assert "reference receiver current" in result.detail
    assert not any(command[0] == "outputd-status" for command in calls)


def test_check_fresh_receiver_preserves_excessive_drift_warning(
    monkeypatch,
    tmp_path: Path,
):
    drift_line = "drained 7 stale ref frames (drift)"
    calls = _install_reference_health_check_fakes(
        monkeypatch,
        tmp_path,
        stats=_reference_input_stats(last_frame_age_ms=100),
        journal="\n".join([drift_line] * 31),
    )
    monkeypatch.setattr(doctor.aec, "_loopback_playback_active", lambda: True)

    result = doctor.aec.check_aec_bridge_output_health()

    assert result.status == "warn"
    assert "ref-drift warnings" in result.detail
    assert "reference receiver current" in result.detail
    assert not any(command[0] == "outputd-status" for command in calls)


# ----------------------------------------- DTLN-aec engine health assessment


def _dtln_loaded_line(size: int = 256) -> str:
    """Synthesize the bridge's successful-load log line in journal
    `--output=cat` format. Matches jasper/cli/aec_bridge.py:~675."""
    return (
        f"2026-05-23 12:47:29,197 aec-bridge INFO "
        f"DTLN-aec engine enabled: size={size}, udp out=127.0.0.1:9878"
    )


def _dtln_failed_line(reason: str = "No such file or directory") -> str:
    """Synthesize the bridge's failed-load log line."""
    return (
        f"2026-05-23 12:47:29,197 aec-bridge WARNING "
        f"JASPER_AEC_DTLN_ENABLED set but DTLN couldn't load: {reason}. "
        f"Continuing with AEC3 only."
    )


def test_assess_dtln_engine_loaded_returns_ok():
    """Happy path: bridge logged a successful engine-init line.
    Doctor reports the engine size for the operator to confirm."""
    r = doctor._assess_dtln_engine(_dtln_loaded_line(size=256))
    assert r.status == "ok"
    assert "loaded" in r.detail.lower()
    assert "size=256" in r.detail


def test_assess_dtln_engine_load_failed_returns_fail():
    """The regression we exist to catch: JASPER_AEC_DTLN_ENABLED=1
    but the engine couldn't load (e.g. /var/lib/jasper/dtln/*.onnx
    missing because install.sh's download failed and the manual SCP
    step didn't happen). Without this check, the operator would
    spend a week analyzing 'DTLN never fires' data without realizing
    the engine never ran."""
    r = doctor._assess_dtln_engine(
        _dtln_failed_line(reason="DTLN ONNX models missing in /var/lib/jasper/dtln")
    )
    assert r.status == "fail"
    assert "couldn't load" in r.detail
    assert "/var/lib/jasper/dtln" in r.detail  # actionable path
    assert "jasper-aec-bridge" in r.detail  # actionable next step


def test_assess_dtln_engine_no_marker_warns():
    """Bridge running but no engine-init marker in the journal
    window — probably means the bridge hasn't restarted since the
    env var was set. Warn with the actionable fix command."""
    r = doctor._assess_dtln_engine("some unrelated log lines\nbridge boot\n")
    assert r.status == "warn"
    assert "systemctl restart jasper-aec-bridge" in r.detail


def test_assess_dtln_engine_picks_most_recent_marker():
    """If the journal window straddles a bridge restart that fixed
    an earlier failure, the LATER successful-load line wins. Reverse
    iteration in _assess_dtln_engine ensures we evaluate newest-first."""
    journal = "\n".join(
        [
            _dtln_failed_line(reason="onnxruntime import failed"),
            "(... operator fixed the venv ...)",
            _dtln_loaded_line(size=256),
        ]
    )
    r = doctor._assess_dtln_engine(journal)
    assert r.status == "ok"


def test_check_dtln_skips_when_env_disabled(monkeypatch):
    """When JASPER_AEC_DTLN_ENABLED is unset (legacy dual-stream
    config), the whole check should skip cleanly without running
    journalctl. This is the common case for non-triple-stream
    installs and must not flap."""
    monkeypatch.delenv("JASPER_AEC_DTLN_ENABLED", raising=False)
    r = doctor.check_aec_bridge_dtln_engine()
    assert r.status == "ok"
    assert "skipped" in r.detail.lower()


def _install_fake_dtln_registry(monkeypatch, tmp_path: Path):
    from jasper.aec_engines import dtln_models

    expected = hashlib.sha256(b"model").hexdigest()

    class _FakeEntry:
        def __init__(self, size: int):
            self.size = size

        def files(self, base_dir=tmp_path):
            base = Path(base_dir)
            return [
                (
                    base / f"dtln_aec_{self.size}_1.onnx",
                    "https://example.invalid/1",
                    expected,
                ),
                (
                    base / f"dtln_aec_{self.size}_2.onnx",
                    "https://example.invalid/2",
                    expected,
                ),
            ]

    entries = {
        128: _FakeEntry(128),
        256: _FakeEntry(256),
    }
    monkeypatch.setattr(dtln_models, "DEFAULT_SIZE", 256)
    monkeypatch.setattr(dtln_models, "REGISTRY", tuple(entries.values()))
    monkeypatch.setattr(dtln_models, "by_size", lambda size: entries.get(size))
    monkeypatch.setenv("JASPER_DTLN_MODEL_DIR", str(tmp_path))


def test_check_dtln_fails_when_enabled_model_file_missing(monkeypatch, tmp_path: Path):
    _install_fake_dtln_registry(monkeypatch, tmp_path)
    monkeypatch.setenv("JASPER_AEC_DTLN_ENABLED", "1")
    (tmp_path / "dtln_aec_256_1.onnx").write_bytes(b"model")

    r = doctor.check_aec_bridge_dtln_engine()

    assert r.status == "fail"
    assert "model files are missing" in r.detail
    assert "dtln_aec_256_2.onnx" in r.detail
    assert "deploy/install.sh" in r.detail


def test_check_dtln_fails_when_enabled_model_hash_mismatches(
    monkeypatch,
    tmp_path: Path,
):
    _install_fake_dtln_registry(monkeypatch, tmp_path)
    monkeypatch.setenv("JASPER_AEC_DTLN_ENABLED", "1")
    (tmp_path / "dtln_aec_256_1.onnx").write_bytes(b"model")
    (tmp_path / "dtln_aec_256_2.onnx").write_bytes(b"wrong-model")

    r = doctor.check_aec_bridge_dtln_engine()

    assert r.status == "fail"
    assert "hashes do not match" in r.detail
    assert "dtln_aec_256_2.onnx" in r.detail
    assert "deploy/install.sh" in r.detail


def test_check_dtln_uses_configured_model_size(monkeypatch, tmp_path: Path):
    _install_fake_dtln_registry(monkeypatch, tmp_path)
    monkeypatch.setenv("JASPER_AEC_DTLN_ENABLED", "1")
    monkeypatch.setenv("JASPER_AEC_DTLN_SIZE", "128")
    monkeypatch.setattr(
        doctor.aec,
        "_run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="inactive"),
    )
    (tmp_path / "dtln_aec_128_1.onnx").write_bytes(b"model")
    (tmp_path / "dtln_aec_128_2.onnx").write_bytes(b"model")

    r = doctor.check_aec_bridge_dtln_engine()

    assert r.status == "ok"
    assert "bridge not running" in r.detail


def test_check_dtln_fails_when_configured_model_size_is_invalid(
    monkeypatch,
    tmp_path: Path,
):
    _install_fake_dtln_registry(monkeypatch, tmp_path)
    monkeypatch.setenv("JASPER_AEC_DTLN_ENABLED", "1")
    monkeypatch.setenv("JASPER_AEC_DTLN_SIZE", "large")

    r = doctor.check_aec_bridge_dtln_engine()

    assert r.status == "fail"
    assert "JASPER_AEC_DTLN_SIZE" in r.detail
    assert "not an integer" in r.detail


def test_check_dtln_fails_when_configured_model_size_is_not_registered(
    monkeypatch,
    tmp_path: Path,
):
    _install_fake_dtln_registry(monkeypatch, tmp_path)
    monkeypatch.setenv("JASPER_AEC_DTLN_ENABLED", "1")
    monkeypatch.setenv("JASPER_AEC_DTLN_SIZE", "512")

    r = doctor.check_aec_bridge_dtln_engine()

    assert r.status == "fail"
    assert "JASPER_AEC_DTLN_SIZE=512" in r.detail
    assert "not registered" in r.detail
    assert "128" in r.detail
    assert "256" in r.detail


# ---------------------------------------------------------------------------
# Audio profile runtime truth — shared classifier used by /aec and doctor
# ---------------------------------------------------------------------------


def test_audio_profile_doctor_check_reports_active_chip_profile(monkeypatch):
    monkeypatch.setattr(doctor.aec, "_aec_mode_setting", lambda: "auto")
    settings = {
        "JASPER_WAKE_LEG_RAW": True,
        "JASPER_WAKE_LEG_DTLN": False,
        "JASPER_WAKE_LEG_CHIP_AEC": True,
    }
    monkeypatch.setattr(
        doctor.aec,
        "_wake_leg_setting",
        lambda key, default: settings.get(key, default),
    )

    status = doctor._audio_profile_status_for_doctor(
        bridge_active=True,
        env={
            "JASPER_AUDIO_DAC_ID": "hifiberry_dac8x",
            "JASPER_MIC_DEVICE": "udp:9876",
            "JASPER_AEC_MIC_DEVICE": "Array",
            "JASPER_AEC_CHIP_AEC_ENABLED": "1",
            "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS": "ready",
            "JASPER_MIC_DEVICE_CHIP_AEC_150": "udp:9887",
            "JASPER_MIC_DEVICE_CHIP_AEC_210": "udp:9888",
        },
        mic_probe=MicProbe(
            xvf_present=True,
            capture_channels=6,
            recommended_channels=6,
            variant_id="xvf3800_legacy_square_6ch",
            geometry="square",
            chip_beam_plan="xvf_square_fixed_150_210",
        ),
    )
    result = doctor._assess_audio_profile(status)

    assert result.status == "ok"
    assert "requested=xvf_chip_aec" in result.detail
    assert "active=xvf_chip_aec" in result.detail
    assert "Chip AEC 150 beam via :9876" in result.detail


def test_aec_bridge_running_reports_chip_forwarding(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd == ["systemctl", "is-active", "jasper-aec-bridge.service"]:
            return SimpleNamespace(returncode=0, stdout="active\n", stderr="")
        if cmd == ["systemctl", "is-enabled", "jasper-aec-bridge.service"]:
            return SimpleNamespace(returncode=0, stdout="enabled\n", stderr="")
        raise AssertionError(f"unexpected command: {cmd!r}")

    monkeypatch.setattr(doctor.aec, "_parked_as_bonded_follower", lambda: False)
    monkeypatch.setattr(doctor.aec, "_run", fake_run)
    monkeypatch.setattr(
        doctor.aec,
        "_audio_profile_status_for_doctor",
        lambda *, bridge_active=None: {
            "audio_profile": {"active": "xvf_chip_aec"},
            "microphone": {"processing_mode": "Chip-AEC"},
            "chip_aec_gate": {"status": "approved", "source": "static"},
        },
    )

    result = doctor.aec.check_aec_bridge_running()

    assert result.status == "ok"
    assert "chip-AEC beam forwarding" in result.detail
    assert "WebRTC AEC3 bypassed" in result.detail
    assert "gate=approved/static" in result.detail
    assert "software AEC enabled" not in result.detail


def test_aec_bridge_reports_expected_commissioning_park(monkeypatch):
    from jasper.mics import xvf3800

    def fake_run(cmd, **kwargs):
        if cmd == ["systemctl", "is-active", "jasper-aec-bridge.service"]:
            return SimpleNamespace(returncode=3, stdout="inactive\n", stderr="")
        if cmd == ["systemctl", "is-enabled", "jasper-aec-bridge.service"]:
            return SimpleNamespace(returncode=0, stdout="enabled\n", stderr="")
        raise AssertionError(f"unexpected command: {cmd!r}")

    monkeypatch.setattr(doctor.aec, "_parked_as_bonded_follower", lambda: False)
    monkeypatch.setattr(doctor.aec, "_run", fake_run)
    monkeypatch.setattr(doctor.aec, "_aec_mode_setting", lambda: "auto")
    monkeypatch.setattr(xvf3800, "capture_channels", lambda: 6)
    monkeypatch.setattr(
        doctor.aec,
        "_audio_profile_status_for_doctor",
        lambda *, bridge_active=None: {
            "audio_profile": {
                "state": "commission_required",
                "reason": "alignment artifact is missing",
                "action": "Run sudo jasper-aec-commission",
            },
        },
    )

    result = doctor.aec.check_aec_bridge_running()

    assert result.status == "warn"
    assert "intentionally parked" in result.detail
    assert "alignment artifact is missing" in result.detail
    assert "Run sudo jasper-aec-commission" in result.detail


def test_aec_bridge_reports_deferred_outputd_park_without_blaming_the_bridge(
    monkeypatch,
):
    # `deferred` is aec-init's ordering-guard park: the live outputd has not
    # loaded /var/lib/jasper/outputd.env. Reporting it as `fail` would name
    # jasper-aec-bridge and recommend `journalctl -u jasper-aec-bridge` plus a
    # reconciler restart — the first is empty of anything relevant and the second
    # just re-defers. It must be a warn carrying the reconciler's own words.
    from jasper.mics import xvf3800

    def fake_run(cmd, **kwargs):
        if cmd == ["systemctl", "is-active", "jasper-aec-bridge.service"]:
            return SimpleNamespace(returncode=3, stdout="inactive\n", stderr="")
        if cmd == ["systemctl", "is-enabled", "jasper-aec-bridge.service"]:
            return SimpleNamespace(returncode=0, stdout="enabled\n", stderr="")
        raise AssertionError(f"unexpected command: {cmd!r}")

    monkeypatch.setattr(doctor.aec, "_parked_as_bonded_follower", lambda: False)
    monkeypatch.setattr(doctor.aec, "_run", fake_run)
    monkeypatch.setattr(doctor.aec, "_aec_mode_setting", lambda: "auto")
    monkeypatch.setattr(xvf3800, "capture_channels", lambda: 6)
    monkeypatch.setattr(
        doctor.aec,
        "_audio_profile_status_for_doctor",
        lambda *, bridge_active=None: {
            "audio_profile": {
                "state": "deferred",
                "reason": "jasper-outputd has not loaded the current output declaration",
                "action": "Wait for jasper-outputd to restart, then run the reconciler",
            },
        },
    )

    result = doctor.aec.check_aec_bridge_running()

    assert result.status == "warn"
    assert "intentionally parked" in result.detail
    # The reconciler's reason and action, verbatim.
    assert (
        "jasper-outputd has not loaded the current output declaration" in result.detail
    )
    assert (
        "Wait for jasper-outputd to restart, then run the reconciler" in result.detail
    )
    # None of the bridge-failure remedy leaks through.
    assert "journalctl -u jasper-aec-bridge" not in result.detail
    assert "jasper-aec-commission" not in result.detail


def test_intentional_bridge_parks_match_the_reconciler_dispositions():
    # Both members are load-bearing: dropping either turns a deliberate park into
    # a hard FAIL naming the wrong subsystem.
    assert doctor.aec._INTENTIONAL_PARKS == frozenset(
        {"commission_required", "deferred"}
    )
    reconciler = (
        Path(doctor.aec.__file__).resolve().parents[3]
        / "deploy"
        / "bin"
        / "jasper-aec-reconcile"
    ).read_text(encoding="utf-8")
    for disposition in doctor.aec._INTENTIONAL_PARKS:
        assert f'write_alignment_env \\\n            "{disposition}"' in reconciler


def test_audio_profile_doctor_check_warns_when_runtime_env_pending(monkeypatch):
    monkeypatch.setattr(doctor.aec, "_aec_mode_setting", lambda: "auto")
    settings = {
        "JASPER_WAKE_LEG_RAW": True,
        "JASPER_WAKE_LEG_DTLN": False,
        "JASPER_WAKE_LEG_CHIP_AEC": True,
    }
    monkeypatch.setattr(
        doctor.aec,
        "_wake_leg_setting",
        lambda key, default: settings.get(key, default),
    )

    status = doctor._audio_profile_status_for_doctor(
        bridge_active=True,
        env={
            "JASPER_AUDIO_DAC_ID": "hifiberry_dac8x",
            "JASPER_MIC_DEVICE": "udp:9876",
            "JASPER_AEC_MIC_DEVICE": "Array",
            "JASPER_AEC_CHIP_AEC_ENABLED": "0",
            "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS": "commission_required",
            "JASPER_AEC_CHIP_AEC_ALIGNMENT_REASON": "alignment artifact is missing",
            "JASPER_AEC_CHIP_AEC_ALIGNMENT_ACTION": "Run sudo jasper-aec-commission",
            "JASPER_MIC_DEVICE_CHIP_AEC_150": "",
            "JASPER_MIC_DEVICE_CHIP_AEC_210": "",
        },
        mic_probe=MicProbe(
            xvf_present=True,
            capture_channels=6,
            recommended_channels=6,
            variant_id="xvf3800_legacy_square_6ch",
            geometry="square",
            chip_beam_plan="xvf_square_fixed_150_210",
        ),
    )
    result = doctor._assess_audio_profile(status)

    assert result.status == "warn"
    assert "active=none" in result.detail
    assert "alignment artifact is missing" in result.detail
    assert "action=Run sudo jasper-aec-commission" in result.detail


def test_audio_profile_doctor_check_names_stale_saved_aec_card(monkeypatch):
    monkeypatch.setattr(doctor.aec, "_aec_mode_setting", lambda: "auto")
    settings = {
        "JASPER_WAKE_LEG_RAW": False,
        "JASPER_WAKE_LEG_DTLN": False,
        "JASPER_WAKE_LEG_CHIP_AEC": True,
    }
    monkeypatch.setattr(
        doctor.aec,
        "_wake_leg_setting",
        lambda key, default: settings.get(key, default),
    )

    status = doctor._audio_profile_status_for_doctor(
        bridge_active=False,
        env={
            "JASPER_AUDIO_DAC_ID": "hifiberry_dac8x",
            "JASPER_MIC_DEVICE": "udp:9876",
            "JASPER_AEC_MIC_DEVICE": "L16K6Ch",
            "JASPER_AEC_CHIP_AEC_ENABLED": "0",
        },
        mic_probe=MicProbe(
            xvf_present=True,
            capture_channels=6,
            recommended_channels=6,
            alsa_card_name="Array",
            variant_id="xvf3800_legacy_square_6ch",
            geometry="square",
            chip_beam_plan="xvf_square_fixed_150_210",
        ),
    )
    result = doctor._assess_audio_profile(status)

    assert result.status == "warn"
    assert "Configured AEC mic L16K6Ch" in result.detail
    assert "detected XVF card Array" in result.detail


def test_audio_validation_advisory_ok_when_chip_aec_not_requested():
    result = doctor._assess_audio_validation_summary(
        {
            "state": "missing",
            "status": "unknown",
            "artifact_path": "/var/lib/jasper/audio-validation",
            "reason": "artifact not found",
        },
        requested_profile="xvf_software_aec3",
    )

    assert result.status == "ok"
    assert "advisory" in result.detail


def test_audio_validation_warns_when_chip_aec_requested_and_missing():
    result = doctor._assess_audio_validation_summary(
        {
            "state": "missing",
            "status": "unknown",
            "artifact_path": "/var/lib/jasper/audio-validation",
            "reason": "artifact not found",
        },
        requested_profile="xvf_chip_aec",
    )

    assert result.status == "warn"
    assert "sudo jasper-audio-validate --stdout" in result.detail
    assert "advisory" in result.detail


def test_audio_validation_suggests_hardware_runner_when_ready_for_passive_evidence():
    result = doctor._assess_audio_validation_summary(
        {
            "state": "current",
            "status": "warn",
            "recommendation": "run_hardware_validation",
            "artifact_path": "/var/lib/jasper/audio-validation/latest.json",
        },
        requested_profile="xvf_chip_aec",
    )

    assert result.status == "warn"
    assert (
        "sudo jasper-audio-hw-validate --duration-seconds 10 --stdout" in result.detail
    )
    assert "advisory" in result.detail


def test_audio_validation_suggests_hardware_runner_for_drift_delay_recommendation():
    result = doctor._assess_audio_validation_summary(
        {
            "state": "current",
            "status": "warn",
            "recommendation": "run_drift_delay_validation",
            "artifact_path": "/var/lib/jasper/audio-validation/latest.json",
        },
        requested_profile="xvf_chip_aec",
    )

    assert result.status == "warn"
    assert (
        "sudo jasper-audio-hw-validate --duration-seconds 10 --stdout" in result.detail
    )


def test_audio_validation_ok_for_known_supported_passive_chip_aec_validation():
    result = doctor._assess_audio_validation_summary(
        {
            "state": "current",
            "status": "warn",
            "recommendation": "run_drift_delay_validation",
            "artifact_path": "/var/lib/jasper/audio-validation/latest.json",
            "hardware": {
                "mic_id": "xvf3800",
                "dac_id": "hifiberry_dac8x",
            },
            "check_statuses": {
                "runtime_profile": "pass",
                "mic_detected": "pass",
                "runtime_env": "pass",
                "service_state": "pass",
                "dac_reference": "pass",
                "wake_legs": "pass",
                "bridge_counters": "warn",
                "outputd_reference_health": "pass",
                "bridge_counter_window": "pass",
                "chip_profile_readback": "pass",
                "chip_convergence": "pass",
                "measured_drift_delay": "not_run",
            },
        },
        requested_profile="xvf_chip_aec",
    )

    assert result.status == "ok"
    assert "known-supported xvf_chip_aec path" in result.detail
    assert "optional acoustic drift/delay probe" in result.detail


def test_audio_validation_still_warns_for_unknown_dac_drift_delay_recommendation():
    result = doctor._assess_audio_validation_summary(
        {
            "state": "current",
            "status": "warn",
            "recommendation": "run_drift_delay_validation",
            "artifact_path": "/var/lib/jasper/audio-validation/latest.json",
            "hardware": {
                "mic_id": "xvf3800",
                "dac_id": "apple_usb_c_dongle",
            },
            "check_statuses": {
                "runtime_profile": "pass",
                "mic_detected": "pass",
                "runtime_env": "pass",
                "service_state": "pass",
                "dac_reference": "pass",
                "wake_legs": "pass",
                "outputd_reference_health": "pass",
                "bridge_counter_window": "pass",
                "chip_profile_readback": "pass",
                "chip_convergence": "pass",
                "measured_drift_delay": "not_run",
            },
        },
        requested_profile="xvf_chip_aec",
    )

    assert result.status == "warn"
    assert (
        "sudo jasper-audio-hw-validate --duration-seconds 10 --stdout" in result.detail
    )


def test_audio_validation_readiness_filters_current_hardware(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        doctor.aec,
        "_audio_profile_status_for_doctor",
        lambda: {"audio_profile": {"requested": "xvf_chip_aec"}},
    )
    monkeypatch.setattr(
        doctor.aec,
        "_shared_parse_env_file",
        lambda _path: {"JASPER_AUDIO_DAC_ID": "apple_usb_c_dongle"},
    )
    monkeypatch.setattr(
        doctor.aec,
        "_audio_validation_filter_kwargs",
        lambda **kwargs: {
            "requested_profile": kwargs["requested_profile"],
            "mic_id": "xvf3800",
            "dac_id": "apple_usb_c_dongle",
        },
    )

    def fake_summary(**kwargs):
        captured.update(kwargs)
        return {
            "state": "current",
            "status": "pass",
            "artifact_path": "/var/lib/jasper/audio-validation/latest.json",
        }

    monkeypatch.setattr(doctor.aec, "_audio_validation_summary", fake_summary)

    result = doctor.check_audio_validation_readiness()

    assert result.status == "ok"
    assert captured == {
        "requested_profile": "xvf_chip_aec",
        "mic_id": "xvf3800",
        "dac_id": "apple_usb_c_dongle",
    }


# DTLN engine — bridge stats snapshot surface (journal-independent)
# ---------------------------------------------------------------------------


def _dtln_stats(enabled: bool, loaded: bool, error=None, age_sec: float = 1.0):
    import time as _time

    return {
        "schema_version": 1,
        "updated_epoch_sec": _time.time() - age_sec,
        "leg_engines": {
            "dtln": {"enabled": enabled, "loaded": loaded, "error": error},
        },
    }


def test_assess_dtln_stats_loaded_returns_ok():
    import time as _time

    r = doctor.aec._assess_dtln_engine_from_stats(
        _dtln_stats(enabled=True, loaded=True),
        _time.time(),
    )
    assert r is not None and r.status == "ok"
    assert "stats snapshot" in r.detail


def test_assess_dtln_stats_load_failure_returns_fail_with_detail():
    import time as _time

    r = doctor.aec._assess_dtln_engine_from_stats(
        _dtln_stats(enabled=True, loaded=False, error="onnx missing"),
        _time.time(),
    )
    assert r is not None and r.status == "fail"
    assert "onnx missing" in r.detail
    assert "engine unavailable" in r.detail
    assert "could not load" not in r.detail
    assert ":9878" in r.detail  # names the unfed leg voice listens on


def test_assess_dtln_stats_bridge_started_without_leg_warns():
    import time as _time

    r = doctor.aec._assess_dtln_engine_from_stats(
        _dtln_stats(enabled=False, loaded=False),
        _time.time(),
    )
    assert r is not None and r.status == "warn"
    assert "systemctl restart jasper-aec-bridge" in r.detail
    # A hand-set JASPER_AEC_DTLN_ENABLED=1 under the chip-AEC profile is
    # NOT a stale-restart problem — the chip profile never loads DTLN.
    # The message must point at checking the active input profile, not
    # only at restarting the bridge.
    assert "input profile" in r.detail
    assert "xvf_chip_aec" in r.detail


def test_assess_dtln_stats_stale_or_legacy_falls_back():
    import time as _time

    now = _time.time()
    # Stale snapshot (dead/old bridge process) → journal fallback.
    assert (
        doctor.aec._assess_dtln_engine_from_stats(
            _dtln_stats(enabled=True, loaded=True, age_sec=120.0),
            now,
        )
        is None
    )
    # Pre-leg_engines bridge build → journal fallback.
    assert (
        doctor.aec._assess_dtln_engine_from_stats(
            {"updated_epoch_sec": now},
            now,
        )
        is None
    )


def test_check_dtln_prefers_stats_snapshot_over_journal(
    monkeypatch,
    tmp_path: Path,
):
    """End-to-end: with a fresh stats snapshot reporting a load
    failure, the check fails from the snapshot and never shells out
    to journalctl (whose 10-min window would miss an old failure)."""
    _install_fake_dtln_registry(monkeypatch, tmp_path)
    monkeypatch.setenv("JASPER_AEC_DTLN_ENABLED", "1")
    (tmp_path / "dtln_aec_256_1.onnx").write_bytes(b"model")
    (tmp_path / "dtln_aec_256_2.onnx").write_bytes(b"model")
    stats_path = tmp_path / "aec_bridge_stats.json"
    stats_path.write_text(
        json.dumps(
            _dtln_stats(enabled=True, loaded=False, error="no onnxruntime"),
        )
    )
    monkeypatch.setenv("JASPER_AEC_BRIDGE_STATS_PATH", str(stats_path))

    def _fake_run(cmd, **kwargs):
        if cmd[0] == "systemctl":
            return SimpleNamespace(stdout="active", stderr="", returncode=0)
        raise AssertionError(f"unexpected subprocess: {cmd}")

    monkeypatch.setattr(doctor.aec, "_run", _fake_run)

    r = doctor.check_aec_bridge_dtln_engine()

    assert r.status == "fail"
    assert "no onnxruntime" in r.detail


# --- Optional enhanced AEC: requested-only advisory -----------------


def test_enhanced_aec_doctor_is_quiet_and_cheap_when_not_requested(monkeypatch):
    monkeypatch.setattr(
        doctor.aec.enhanced_aec,
        "read_intent",
        lambda: {"requested": False},
    )

    def unexpected_status(**_kwargs):
        raise AssertionError("status/fingerprint work must stay requested-only")

    monkeypatch.setattr(doctor.aec.enhanced_aec, "status", unexpected_status)

    result = doctor.check_enhanced_aec()

    assert result.status == "ok"
    assert "not requested" in result.detail


@pytest.mark.parametrize("state", ["installed", "not_needed", "installing"])
def test_enhanced_aec_doctor_accepts_non_actionable_states(
    monkeypatch,
    state,
):
    monkeypatch.setattr(
        doctor.aec.enhanced_aec,
        "read_intent",
        lambda: {"requested": True},
    )
    monkeypatch.setattr(
        doctor.aec,
        "_audio_profile_status_for_doctor",
        lambda: {"audio_profile": {"active": "xvf_software_aec3"}},
    )
    monkeypatch.setattr(
        doctor.aec,
        "_run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [],
            0,
            stdout="inactive\n",
            stderr="",
        ),
    )
    monkeypatch.setattr(
        doctor.aec.enhanced_aec,
        "status",
        lambda **_kwargs: {"state": state, "detail": f"state={state}"},
    )

    result = doctor.check_enhanced_aec()

    assert result.status == "ok"
    assert result.detail == f"state={state}"


@pytest.mark.parametrize(
    "state",
    ["not_installed", "failed", "stale", "unavailable"],
)
def test_enhanced_aec_doctor_warns_only_after_request(monkeypatch, state):
    monkeypatch.setattr(
        doctor.aec.enhanced_aec,
        "read_intent",
        lambda: {"requested": True},
    )
    monkeypatch.setattr(
        doctor.aec,
        "_audio_profile_status_for_doctor",
        lambda: {"audio_profile": {"active": "xvf_software_aec3"}},
    )
    monkeypatch.setattr(
        doctor.aec,
        "_run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [],
            0,
            stdout="inactive\n",
            stderr="",
        ),
    )
    monkeypatch.setattr(
        doctor.aec.enhanced_aec,
        "status",
        lambda **_kwargs: {"state": state, "detail": f"state={state}"},
    )

    result = doctor.check_enhanced_aec()

    assert result.status == "warn"
    assert "standard echo cancellation remains available" in result.detail
    assert "/system/" in result.detail
