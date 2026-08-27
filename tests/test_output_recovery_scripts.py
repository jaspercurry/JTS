# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
FAILURE_RECONCILE = REPO / "deploy" / "bin" / "jasper-outputd-failure-reconcile"
OUTPUTD_UNIT = REPO / "deploy" / "systemd" / "jasper-outputd.service"

def _write_executable(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


class _FailureReconcileHarness:
    """Run jasper-outputd-failure-reconcile against fake collaborators."""

    def __init__(self, tmp_path: Path) -> None:
        self.reconcile_log = tmp_path / "reconcile.log"
        self.systemctl_log = tmp_path / "systemctl.log"
        self.reconcile_rc = tmp_path / "reconcile.rc"
        self.reconcile_rc.write_text("0", encoding="utf-8")
        fake_reconcile = _write_executable(
            tmp_path / "jasper-audio-hardware-reconcile",
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$*\" >> \"$JASPER_TEST_LOG\"\n"
            'exit "$(cat "$JASPER_TEST_RECONCILE_RC")"\n',
        )
        fake_systemctl = _write_executable(
            tmp_path / "systemctl",
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$*\" >> \"$JASPER_SYSTEMCTL_LOG\"\n",
        )
        self.env = os.environ.copy()
        self.env.update({
            "JASPER_AUDIO_HARDWARE_RECONCILE": str(fake_reconcile),
            "JASPER_SYSTEMCTL": str(fake_systemctl),
            "JASPER_TEST_LOG": str(self.reconcile_log),
            "JASPER_TEST_RECONCILE_RC": str(self.reconcile_rc),
            "JASPER_SYSTEMCTL_LOG": str(self.systemctl_log),
            "JASPER_OUTPUTD_CONFIG_RETRY_STATE": str(tmp_path / "config-retry.stamp"),
        })

    def run(
        self,
        *,
        result: str = "exit-code",
        exit_status: str = "1",
        **env: str,
    ) -> subprocess.CompletedProcess[str]:
        run_env = dict(self.env)
        run_env.update({"SERVICE_RESULT": result, "EXIT_STATUS": exit_status})
        run_env.update(env)
        return subprocess.run(
            [str(FAILURE_RECONCILE)],
            env=run_env,
            text=True,
            capture_output=True,
            check=True,
        )

    def systemctl_calls(self) -> list[str]:
        if not self.systemctl_log.exists():
            return []
        return self.systemctl_log.read_text(encoding="utf-8").splitlines()


def test_output_hardware_hotplug_requests_reconcile_without_blocking(
    tmp_path: Path,
) -> None:
    log = tmp_path / "systemctl.log"
    fake_systemctl = _write_executable(
        tmp_path / "systemctl",
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$JASPER_TEST_LOG\"\n",
    )

    env = os.environ.copy()
    env.update({
        "JASPER_SYSTEMCTL": str(fake_systemctl),
        "JASPER_TEST_LOG": str(log),
        "ACTION": "remove",
        "SUBSYSTEM": "usb",
        "PRODUCT": "5ac/110a/100",
    })

    result = subprocess.run(
        [str(REPO / "deploy" / "bin" / "jasper-output-hardware-hotplug")],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert log.read_text(encoding="utf-8").strip() == (
        "--no-block start jasper-audio-hardware-reconcile.service"
    )
    assert "event=audio_hardware_hotplug.reconcile_requested" in result.stderr


def test_outputd_failure_reconcile_refreshes_env_for_retry(tmp_path: Path) -> None:
    log = tmp_path / "reconcile.log"
    fake_reconcile = _write_executable(
        tmp_path / "jasper-audio-hardware-reconcile",
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$JASPER_TEST_LOG\"\n",
    )

    env = os.environ.copy()
    env.update({
        "JASPER_AUDIO_HARDWARE_RECONCILE": str(fake_reconcile),
        "JASPER_TEST_LOG": str(log),
        "JASPER_JOURNALCTL": "/bin/false",
        "SERVICE_RESULT": "exit-code",
        "EXIT_STATUS": "1",
    })

    result = subprocess.run(
        [str(REPO / "deploy" / "bin" / "jasper-outputd-failure-reconcile")],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert log.read_text(encoding="utf-8").strip() == (
        "--reason outputd-failure --no-restart"
    )
    assert "event=outputd.failure_reconcile.ok" in result.stderr


def test_outputd_failure_reconcile_retries_config_exit_once(tmp_path: Path) -> None:
    log = tmp_path / "reconcile.log"
    systemctl_log = tmp_path / "systemctl.log"
    fake_reconcile = _write_executable(
        tmp_path / "jasper-audio-hardware-reconcile",
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$JASPER_TEST_LOG\"\n",
    )
    fake_systemctl = _write_executable(
        tmp_path / "systemctl",
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$JASPER_SYSTEMCTL_LOG\"\n",
    )

    env = os.environ.copy()
    env.update({
        "JASPER_AUDIO_HARDWARE_RECONCILE": str(fake_reconcile),
        "JASPER_SYSTEMCTL": str(fake_systemctl),
        "JASPER_TEST_LOG": str(log),
        "JASPER_SYSTEMCTL_LOG": str(systemctl_log),
        "JASPER_OUTPUTD_CONFIG_RETRY_STATE": str(tmp_path / "config-retry.stamp"),
        "SERVICE_RESULT": "exit-code",
        "EXIT_STATUS": "78",
    })

    result = subprocess.run(
        [str(REPO / "deploy" / "bin" / "jasper-outputd-failure-reconcile")],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert log.read_text(encoding="utf-8").strip() == (
        "--reason outputd-config-failure --no-restart"
    )
    assert systemctl_log.read_text(encoding="utf-8").splitlines() == [
        "reset-failed jasper-outputd.service",
        "--no-block restart jasper-outputd.service",
    ]
    assert "event=outputd.failure_reconcile.retry" in result.stderr
    assert "reason=config_reconciled" in result.stderr

    log.write_text("", encoding="utf-8")
    systemctl_log.write_text("", encoding="utf-8")
    second = subprocess.run(
        [str(REPO / "deploy" / "bin" / "jasper-outputd-failure-reconcile")],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert log.read_text(encoding="utf-8") == ""
    assert systemctl_log.read_text(encoding="utf-8") == ""
    assert "event=outputd.failure_reconcile.skip" in second.stderr
    assert "reason=config_retry_already_attempted" in second.stderr


def test_outputd_failure_reconcile_skips_normal_stops(tmp_path: Path) -> None:
    log = tmp_path / "reconcile.log"
    fake_reconcile = _write_executable(
        tmp_path / "jasper-audio-hardware-reconcile",
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$JASPER_TEST_LOG\"\n",
    )

    env = os.environ.copy()
    env.update({
        "JASPER_AUDIO_HARDWARE_RECONCILE": str(fake_reconcile),
        "JASPER_TEST_LOG": str(log),
        "SERVICE_RESULT": "success",
        "EXIT_STATUS": "0",
    })

    result = subprocess.run(
        [str(REPO / "deploy" / "bin" / "jasper-outputd-failure-reconcile")],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert not log.exists()
    assert "event=outputd.failure_reconcile.skip" in result.stderr
    assert "reason=non_retrying_stop" in result.stderr


def test_outputd_failure_reconcile_skips_an_exec_condition_park(
    tmp_path: Path,
) -> None:
    """`exec-condition` is systemd's spelling; `condition` matched nothing."""
    harness = _FailureReconcileHarness(tmp_path)

    result = harness.run(result="exec-condition", exit_status="1")

    assert not harness.reconcile_log.exists()
    assert "event=outputd.failure_reconcile.skip" in result.stderr
    assert "reason=non_retrying_stop" in result.stderr

