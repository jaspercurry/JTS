# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
FAILURE_RECONCILE = REPO / "deploy" / "bin" / "jasper-outputd-failure-reconcile"
UNPARK = REPO / "deploy" / "bin" / "jasper-unpark"

def _write_executable(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


class _FailureReconcileHarness:
    """Run jasper-outputd-failure-reconcile against a fake systemctl."""

    def __init__(self, tmp_path: Path) -> None:
        self.systemctl_log = tmp_path / "systemctl.log"
        fake_systemctl = _write_executable(
            tmp_path / "systemctl",
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$*\" >> \"$JASPER_SYSTEMCTL_LOG\"\n",
        )
        self.park = tmp_path / "failure-reconcile.park"
        self.env = os.environ.copy()
        self.env.update({
            "JASPER_SYSTEMCTL": str(fake_systemctl),
            "JASPER_SYSTEMCTL_LOG": str(self.systemctl_log),
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


_RECONCILE_REQUEST = "--no-block start jasper-audio-hardware-reconcile.service"


@pytest.mark.parametrize(
    ("result", "exit_status", "park_reason"),
    [
        ("exit-code", "78", "config_exit"),
        ("exit-code", "1", None),
        ("signal", "KILL", None),
    ],
    ids=["config-exit-parks", "crash", "signal"],
)
def test_outputd_failure_reconcile_requests_a_reconcile_pass(
    tmp_path: Path, result: str, exit_status: str, park_reason: str | None
) -> None:
    """A failing stop starts the reconcile unit and runs no pass in outputd's
    sandbox. Only exit 78, which RestartPreventExitStatus= holds, is a park
    (ADR-0409)."""
    harness = _FailureReconcileHarness(tmp_path)

    harness.run(result=result, exit_status=exit_status)

    assert harness.systemctl_calls() == [_RECONCILE_REQUEST]
    record = harness.park_record()
    assert record.get("reason") == park_reason
    if park_reason:
        assert record["exit_status"] == "78"
        assert int(record["parked_at"]) > 0


@pytest.mark.parametrize("result", ["success", "exec-condition"])
def test_outputd_failure_reconcile_skips_non_retrying_stops(
    tmp_path: Path, result: str
) -> None:
    harness = _FailureReconcileHarness(tmp_path)

    harness.run(result=result, exit_status="0")

    assert harness.systemctl_calls() == []
    assert harness.park_record() == {}


def test_outputd_failure_reconcile_is_fail_open(tmp_path: Path) -> None:
    """A reconcile request that cannot be sent still records the park and
    exits 0."""
    harness = _FailureReconcileHarness(tmp_path)

    harness.run(exit_status="78", JASPER_SYSTEMCTL=str(tmp_path / "absent"))

    assert harness.park_record()["reason"] == "config_exit"


# ----------------------------------------------------------- jasper-unpark


def _unpark(park: Path, event: str = "outputd.unparked") -> list[str]:
    """The argv jasper-outputd.service's ExecStartPost= builds."""
    return [str(UNPARK), str(park), event]


def test_unpark_is_a_noop_with_no_park_record(tmp_path: Path) -> None:
    park = tmp_path / "failure-reconcile.park"

    result = subprocess.run(
        _unpark(park), text=True, capture_output=True, check=True,
    )

    assert result.stderr == ""
    assert not park.exists()
    assert not Path(str(park) + ".last").exists()


def test_unpark_copies_the_record_to_last_with_unparked_at_then_removes_it(
    tmp_path: Path,
) -> None:
    park = tmp_path / "failure-reconcile.park"
    park.write_text("parked_at=1000\nexit_status=78\nreason=recent\n")

    result = subprocess.run(
        _unpark(park), text=True, capture_output=True, check=True,
    )

    assert not park.exists()
    last = tmp_path / "failure-reconcile.park.last"
    fields = dict(
        line.split("=", 1)
        for line in last.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    assert fields["parked_at"] == "1000"
    assert fields["exit_status"] == "78"
    assert fields["reason"] == "recent"
    assert int(fields["unparked_at"]) > 0
    assert f"event=outputd.unparked state={last} preserved=1" in result.stderr


def test_unpark_journals_preserved_0_when_the_last_write_fails(
    tmp_path: Path,
) -> None:
    """preserved= reflects the ACTUAL .last write result, not an assumption:
    a directory the script cannot write into must still remove the live park
    record (fail-open) but journal preserved=0, not silently claim success."""
    park_dir = tmp_path / "state"
    park_dir.mkdir()
    park = park_dir / "failure-reconcile.park"
    park.write_text("parked_at=1000\nexit_status=78\nreason=recent\n")
    park_dir.chmod(0o500)  # read+execute, no write: tmp/.last/mv all fail

    try:
        result = subprocess.run(
            _unpark(park), text=True, capture_output=True, check=True,
        )
    finally:
        park_dir.chmod(0o700)  # tmp_path cleanup needs write back

    # The dir has no write bit: the final `rm -f` on the live record also
    # fails silently (fail-open), so the record survives — a subsequent boot
    # can still retry the copy rather than the record vanishing unpreserved.
    assert park.exists()
    assert not (park_dir / "failure-reconcile.park.last").exists()
    assert (
        f"event=outputd.unparked state={park_dir / 'failure-reconcile.park.last'} "
        "preserved=0" in result.stderr
    )
