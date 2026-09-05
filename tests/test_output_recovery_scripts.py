# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
FAILURE_RECONCILE = REPO / "deploy" / "bin" / "jasper-outputd-failure-reconcile"

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
        self.stamp = tmp_path / "failure-reconcile.stamp"
        self.park = tmp_path / "failure-reconcile.park"
        self.env = os.environ.copy()
        self.env.update({
            "JASPER_AUDIO_HARDWARE_RECONCILE": str(fake_reconcile),
            "JASPER_SYSTEMCTL": str(fake_systemctl),
            "JASPER_TEST_LOG": str(self.reconcile_log),
            "JASPER_TEST_RECONCILE_RC": str(self.reconcile_rc),
            "JASPER_SYSTEMCTL_LOG": str(self.systemctl_log),
            "JASPER_OUTPUTD_CONFIG_RETRY_STATE": str(self.stamp),
            "JASPER_OUTPUTD_RECONCILE_PARK_STATE": str(self.park),
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

    def park_record(self) -> dict[str, str]:
        if not self.park.exists():
            return {}
        return dict(
            line.split("=", 1)
            for line in self.park.read_text(encoding="utf-8").splitlines()
            if "=" in line
        )

    def reconcile_calls(self) -> list[str]:
        return self._calls(self.reconcile_log)

    def systemctl_calls(self) -> list[str]:
        return self._calls(self.systemctl_log)

    @staticmethod
    def _calls(log: Path) -> list[str]:
        if not log.exists():
            return []
        return log.read_text(encoding="utf-8").splitlines()


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
    harness = _FailureReconcileHarness(tmp_path)

    result = harness.run()

    assert harness.reconcile_calls() == ["--reason outputd-failure --no-restart"]
    assert "event=outputd.failure_reconcile.ok" in result.stderr


def test_outputd_failure_reconcile_runs_once_per_window(tmp_path: Path) -> None:
    """A crash loop reconciles once, not once per restart in the burst."""
    harness = _FailureReconcileHarness(tmp_path)

    harness.run(result="signal", exit_status="KILL")
    second = harness.run(result="signal", exit_status="KILL")

    assert harness.reconcile_calls() == ["--reason outputd-failure --no-restart"]
    assert "event=outputd.failure_reconcile.skip" in second.stderr

    harness.stamp.write_text(str(int(time.time()) - 3600), encoding="utf-8")
    harness.run(result="signal", exit_status="KILL")

    assert harness.reconcile_calls() == ["--reason outputd-failure --no-restart"] * 2


def test_outputd_failure_reconcile_retries_config_exit_once(tmp_path: Path) -> None:
    harness = _FailureReconcileHarness(tmp_path)
    reconciled = ["--reason outputd-config-failure --no-restart"]
    retried = [
        "reset-failed jasper-outputd.service",
        "--no-block restart jasper-outputd.service",
    ]

    result = harness.run(exit_status="78")

    assert harness.reconcile_calls() == reconciled
    assert harness.systemctl_calls() == retried
    assert "event=outputd.failure_reconcile.retry" in result.stderr

    assert harness.park_record() == {}

    second = harness.run(exit_status="78")

    assert harness.reconcile_calls() == reconciled
    assert harness.systemctl_calls() == retried
    assert "event=outputd.failure_reconcile.skip" in second.stderr
    # The retry is spent and RestartPreventExitStatus=78 holds the unit: the
    # actor records the park rather than leaving a reader to infer one.
    record = harness.park_record()
    assert (record["exit_status"], record["reason"]) == ("78", "recent")
    assert int(record["parked_at"]) > 0


def test_outputd_failure_reconcile_records_a_park_with_no_reconciler(
    tmp_path: Path,
) -> None:
    """No reconciler binary means exit 78 got no retry either."""
    harness = _FailureReconcileHarness(tmp_path)

    result = harness.run(
        exit_status="78",
        JASPER_AUDIO_HARDWARE_RECONCILE=str(tmp_path / "absent"),
    )

    assert harness.park_record()["reason"] == "reconciler_unavailable"
    assert "event=outputd.failure_reconcile.park" in result.stderr


def test_outputd_failure_reconcile_records_a_park_when_the_reconciler_fails(
    tmp_path: Path,
) -> None:
    harness = _FailureReconcileHarness(tmp_path)
    harness.reconcile_rc.write_text("3", encoding="utf-8")

    harness.run(exit_status="78")

    assert harness.park_record()["reason"] == "config_reconciler_nonzero"
    assert harness.systemctl_calls() == []


def test_outputd_failure_reconcile_records_no_park_for_other_exits(
    tmp_path: Path,
) -> None:
    """Only exit 78 is held by RestartPreventExitStatus; every other class
    keeps systemd's Restart=on-failure, so it is not a park."""
    harness = _FailureReconcileHarness(tmp_path)

    harness.run(result="signal", exit_status="KILL")
    harness.run(result="signal", exit_status="KILL")

    assert harness.park_record() == {}


def test_outputd_failure_reconcile_skips_normal_stops(tmp_path: Path) -> None:
    harness = _FailureReconcileHarness(tmp_path)

    result = harness.run(result="success", exit_status="0")

    assert harness.reconcile_calls() == []
    assert "event=outputd.failure_reconcile.skip" in result.stderr
    assert "reason=non_retrying_stop" in result.stderr


def test_outputd_failure_reconcile_skips_an_exec_condition_park(
    tmp_path: Path,
) -> None:
    """`exec-condition` is systemd's spelling; `condition` matched nothing."""
    harness = _FailureReconcileHarness(tmp_path)

    result = harness.run(result="exec-condition", exit_status="1")

    assert harness.reconcile_calls() == []
    assert "event=outputd.failure_reconcile.skip" in result.stderr
    assert "reason=non_retrying_stop" in result.stderr

