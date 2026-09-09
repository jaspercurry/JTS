# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free coverage for deploy/bin/jasper-camilla-recover.

Includes the park record's Python surfaces (jasper.control.camilla_recover_state
and jasper-doctor's check_camilla_recover_park), which read what this script
writes.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "bin" / "jasper-camilla-recover"


def _write_exe(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _fake_env(tmp_path: Path) -> tuple[dict[str, str], Path]:
    calls = tmp_path / "systemctl.calls"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    asound = tmp_path / "asound"
    status_dir = asound / "card0" / "pcm0p" / "sub0"
    status_dir.mkdir(parents=True)
    (status_dir / "status").write_text("state: RUNNING\nowner: fake\n", encoding="utf-8")
    dev_snd = tmp_path / "dev_snd"
    dev_snd.mkdir()
    (dev_snd / "pcmC0D0p").write_text("", encoding="utf-8")

    _write_exe(
        bin_dir / "fake-systemctl",
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$*\" >> {calls}\n"
        'if [[ -n "${FAKE_SYSTEMCTL_FAIL:-}" && "$*" == ${FAKE_SYSTEMCTL_FAIL} ]]; then\n'
        "    exit 1\n"
        "fi\n"
        'if [[ "$*" == "show -p NRestarts --value jasper-camilla.service" ]]; then\n'
        '    printf \'%s\\n\' "${FAKE_SYSTEMCTL_NRESTARTS-0}"\n'
        "fi\n"
        "exit 0\n",
    )
    _write_exe(
        bin_dir / "fuser",
        "#!/usr/bin/env bash\necho 'fake-holder fuser'\nexit 0\n",
    )
    _write_exe(
        bin_dir / "lsof",
        "#!/usr/bin/env bash\necho 'fake-holder lsof'\nexit 0\n",
    )

    env = os.environ.copy()
    env.update(
        {
            "JASPER_SYSTEMCTL": str(bin_dir / "fake-systemctl"),
            "JASPER_CAMILLA_RECOVER_RUN_DIR": str(run_dir),
            "JASPER_ASOUND_ROOT": str(asound),
            "JASPER_DEV_SND_ROOT": str(dev_snd),
            # The fake systemctl answers NRestarts instantly, so the production
            # 3 s margin only slows this suite down.
            "JASPER_CAMILLA_RECOVER_LIVENESS_WAIT_SEC": "0",
            "PATH": f"{bin_dir}:{env.get('PATH', '')}",
        }
    )
    return env, calls


def _run(
    env: dict[str, str], reason: str, timeout: float = 10,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), "--reason", reason],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _record_fields(record: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in record.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        fields[key] = value
    return fields


def _units_acted_on(calls: Path) -> set[str]:
    """Every unit the handler issued a systemctl verb against."""
    units: set[str] = set()
    for line in calls.read_text(encoding="utf-8").splitlines():
        units.update(token for token in line.split() if token.endswith(".service"))
    return units


def test_recover_captures_evidence_then_restarts_camilla_once(tmp_path: Path):
    env, calls = _fake_env(tmp_path)

    result = _run(env, "pytest")

    assert result.returncode == 0
    assert "event=camilla.recover.start" in result.stderr
    assert "event=camilla.recover.capture_line label=fuser" in result.stderr
    assert "event=camilla.recover.asound_status_line" in result.stderr
    assert "event=camilla.recover.recovered action=camilla_restarted" in result.stderr

    lines = calls.read_text(encoding="utf-8").splitlines()
    # One bounded attempt on the one unit that failed: no park set, no
    # renderer bounce, no reboot.
    assert lines.count("start jasper-camilla.service") == 1
    assert _units_acted_on(calls) == {"jasper-camilla.service"}
    assert not [line for line in lines if line.startswith("stop ")]
    assert "reboot" not in calls.read_text(encoding="utf-8")


def test_dying_after_fork_parks_instead_of_declaring_recovery(tmp_path: Path):
    """Type=simple returns 0 at fork; a doomed restart must still park."""
    env, calls = _fake_env(tmp_path)
    env["FAKE_SYSTEMCTL_NRESTARTS"] = "1"

    result = _run(env, "pytest")

    assert result.returncode == 0
    assert (
        "event=camilla.recover.liveness_check unit=jasper-camilla.service "
        "nrestarts=1 verdict=died_after_start"
    ) in result.stderr
    assert "event=camilla.recover.park reason=camilla_start_failed" in result.stderr
    assert "event=camilla.recover.recovered" not in result.stderr

    fields = _record_fields(tmp_path / "run" / "jasper-camilla-recover.state")
    assert fields["reason"] == "camilla_start_failed"
    assert "NRestarts=1" in fields["detail"]


def test_inconclusive_liveness_probe_still_declares_recovered(tmp_path: Path):
    """An unreadable NRestarts must not park a graph that might be fine.

    The cost of a false park is a working speaker deaf until a human acts, so
    an inconclusive probe falls back to the recovered leg.
    """
    env, _calls = _fake_env(tmp_path)
    env["FAKE_SYSTEMCTL_NRESTARTS"] = ""

    result = _run(env, "pytest")

    assert result.returncode == 0
    assert "event=camilla.recover.recovered action=camilla_restarted" in result.stderr
    assert "event=camilla.recover.liveness_check" not in result.stderr


def _park(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    """Drive one failed recovery. Returns (env, systemctl-calls, record)."""
    env, calls = _fake_env(tmp_path)
    env["FAKE_SYSTEMCTL_FAIL"] = "start jasper-camilla.service"
    first = _run(env, "park")
    assert first.returncode == 0
    assert "event=camilla.recover.park reason=camilla_start_failed" in first.stderr
    return env, calls, tmp_path / "run" / "jasper-camilla-recover.state"


def test_failed_restart_parks_the_graph_and_disarms_its_trigger(tmp_path: Path):
    """A pass that cannot bring the graph back stops CamillaDSP and records why."""
    _env, calls, record = _park(tmp_path)

    fields = _record_fields(record)
    assert set(fields) == {"parked_utc", "reason", "detail", "action", "re_arm"}
    assert fields["reason"] == "camilla_start_failed"
    assert all(fields[key] for key in fields)

    call_text = calls.read_text(encoding="utf-8")
    # reset-failed + stop is what makes the floor stable: the unit cannot
    # exhaust another burst, so OnFailure= cannot re-enter this handler. The
    # stop is --no-block so a wedged daemon cannot burn the handler's
    # TimeoutStartSec before the record is written.
    assert "--no-block stop jasper-camilla.service" in call_text
    assert "reboot" not in call_text


def test_a_second_trigger_while_parked_is_a_no_op_skip(tmp_path: Path):
    env, calls, _record = _park(tmp_path)
    calls.write_text("", encoding="utf-8")

    second = _run(env, "re-entry")

    assert second.returncode == 0
    assert "event=camilla.recover.suppressed reason=parked" in second.stderr
    assert calls.read_text(encoding="utf-8") == ""


def test_camilla_starting_again_retires_the_park(tmp_path: Path):
    """The cause clearing — CamillaDSP running again — is what ends the park.

    The unit itself retires the record, so a repaired box cannot keep a red
    doctor row, and a later genuinely-new fault gets a full recovery pass.
    """
    from jasper.control import camilla_recover_state

    unit = (ROOT / "deploy" / "systemd" / "jasper-camilla.service").read_text(
        encoding="utf-8"
    )
    post = [
        line.split("=", 1)[1]
        for line in unit.splitlines()
        if line.startswith("ExecStartPost=")
    ]
    assert f"-/bin/rm -f {camilla_recover_state.DEFAULT_STATE_PATH}" in post

    # Writer, unit, and reader must all name the one file.
    _env, _calls, record = _park(tmp_path)
    assert record.name == Path(camilla_recover_state.DEFAULT_STATE_PATH).name
    assert camilla_recover_state.snapshot(str(record))["parked"] is True


def test_park_reason_and_action_reach_the_doctor(tmp_path: Path, monkeypatch):
    """One reader, from the writer's own record to the operator surface."""
    from jasper.cli.doctor import _registry, audio_runtime_camilla
    from jasper.control import camilla_recover_state

    names = [c.func.__name__ for c in _registry.registered_checks()]
    assert "check_camilla_recover_park" in names

    _env, _calls, record = _park(tmp_path)
    monkeypatch.setenv("JASPER_CAMILLA_RECOVER_PARK_STATE", str(record))

    snapshot = camilla_recover_state.snapshot()
    assert snapshot["parked"] is True
    assert snapshot["reason"] == "camilla_start_failed"

    result = audio_runtime_camilla.check_camilla_recover_park()
    assert result.status == "fail"
    assert result.reason == audio_runtime_camilla.REASON_CAMILLA_GRAPH_PARKED
    # The core DSP graph is down: this is exactly the row the dashboard's
    # summary must lead with (AGENTS.md/ADR-0233 rule 3).
    assert result.speaker_silent is True


def test_a_hung_capture_cannot_spend_the_restart_budget(tmp_path: Path):
    """Evidence is bounded so it can never cost the graph its restart attempt.

    One capture has been observed taking 19 s of the handler's deadline, with
    the kill landing before any unit was restarted.
    """
    env, calls = _fake_env(tmp_path)
    _write_exe(tmp_path / "bin" / "lsof", "#!/usr/bin/env bash\nsleep 30\n")

    started = time.monotonic()
    result = _run(env, "hung-capture", timeout=25)
    elapsed = time.monotonic() - started

    assert result.returncode == 0
    assert elapsed < 20
    assert "start jasper-camilla.service" in calls.read_text(encoding="utf-8")
    assert "event=camilla.recover.recovered action=camilla_restarted" in result.stderr
