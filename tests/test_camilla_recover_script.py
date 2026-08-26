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
    state_dir = tmp_path / "state"
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
            "JASPER_CAMILLA_RECOVER_STATE_DIR": str(state_dir),
            "JASPER_CAMILLA_RECOVER_RUN_DIR": str(run_dir),
            "JASPER_ASOUND_ROOT": str(asound),
            "JASPER_DEV_SND_ROOT": str(dev_snd),
            "PATH": f"{bin_dir}:{env.get('PATH', '')}",
        }
    )
    return env, calls


def test_camilla_recover_captures_evidence_and_restarts_core_graph(tmp_path: Path):
    env, calls = _fake_env(tmp_path)

    result = subprocess.run(
        [str(SCRIPT), "--reason", "pytest"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    assert "event=camilla.recover.start" in result.stderr
    assert "event=camilla.recover.capture_line label=fuser" in result.stderr
    assert "event=camilla.recover.asound_status_line" in result.stderr
    assert "event=camilla.recover.recovered action=core_graph_restarted" in result.stderr

    call_text = calls.read_text(encoding="utf-8")
    assert "stop jasper-outputd.service" in call_text
    assert "reset-failed jasper-camilla.service" in call_text
    assert "restart jasper-fanin.service" in call_text
    assert "start jasper-camilla.service" in call_text
    assert "restart jasper-outputd.service" in call_text
    assert "reboot" not in call_text


def test_camilla_recover_cooldown_parks_without_retrying_graph(tmp_path: Path):
    env, calls = _fake_env(tmp_path)
    env["JASPER_CAMILLA_RECOVER_COOLDOWN_SEC"] = "999"

    first = subprocess.run(
        [str(SCRIPT), "--reason", "first"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert first.returncode == 0
    calls.write_text("", encoding="utf-8")

    second = subprocess.run(
        [str(SCRIPT), "--reason", "second"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert second.returncode == 0
    assert "event=camilla.recover.suppressed reason=cooldown" in second.stderr
    call_text = calls.read_text(encoding="utf-8")
    assert "status jasper-camilla.service" in call_text
    assert "start jasper-camilla.service" not in call_text
    assert "restart jasper-outputd.service" not in call_text
    assert "reboot" not in call_text


# --------------------------------------------------------------------------
# The failed-recovery floor (#2564): park once, stay parked, clear on recovery
# --------------------------------------------------------------------------

def _run(env: dict[str, str], reason: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), "--reason", reason],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def _record_fields(record: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in record.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        fields[key] = value
    return fields


def _park(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    """Drive one failed recovery. Returns (env, systemctl-calls, record)."""
    env, calls = _fake_env(tmp_path)
    env["FAKE_SYSTEMCTL_FAIL"] = "start jasper-camilla.service"
    # The park gate, not the cooldown, must be what suppresses re-entry.
    env["JASPER_CAMILLA_RECOVER_COOLDOWN_SEC"] = "0"
    first = _run(env, "park")
    assert first.returncode == 0
    assert "event=camilla.recover.park reason=camilla_start_failed" in first.stderr
    return env, calls, tmp_path / "run" / "jasper-camilla-recover.state"


def test_failed_recovery_parks_the_graph_and_disarms_its_trigger(tmp_path: Path):
    """A recovery that cannot converge stops CamillaDSP and records why."""
    _env, calls, record = _park(tmp_path)

    fields = _record_fields(record)
    assert fields["reason"] == "camilla_start_failed"
    assert fields["action"]
    assert fields["re_arm"]
    assert fields["parked_utc"]

    call_text = calls.read_text(encoding="utf-8")
    # reset-failed + stop is what makes the floor stable: the unit cannot
    # exhaust another burst, so OnFailure= cannot re-enter this handler. The
    # stop is --no-block so a wedged daemon cannot burn the handler's
    # TimeoutStartSec before the record is written.
    assert "--no-block stop jasper-camilla.service" in call_text
    assert "reboot" not in call_text


def test_a_second_trigger_while_parked_is_a_no_op_skip(tmp_path: Path):
    """The bug: re-entry stopped all eleven core-graph units again."""
    env, calls, _record = _park(tmp_path)
    calls.write_text("", encoding="utf-8")

    second = _run(env, "re-entry")

    assert second.returncode == 0
    assert "event=camilla.recover.suppressed reason=parked" in second.stderr
    call_text = calls.read_text(encoding="utf-8")
    assert "stop " not in call_text
    assert "start jasper-camilla.service" not in call_text
    assert "restart jasper-outputd.service" not in call_text


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
    from jasper.cli.doctor import _registry, audio_runtime
    from jasper.control import camilla_recover_state

    names = [c.func.__name__ for c in _registry.registered_checks()]
    assert "check_camilla_recover_park" in names

    _env, _calls, record = _park(tmp_path)
    monkeypatch.setenv("JASPER_CAMILLA_RECOVER_PARK_STATE", str(record))

    snapshot = camilla_recover_state.snapshot()
    assert snapshot["parked"] is True
    assert snapshot["reason"] == "camilla_start_failed"

    result = audio_runtime.check_camilla_recover_park()
    assert result.status == "fail"
    assert snapshot["action"] in result.detail
    assert snapshot["re_arm"] in result.detail
