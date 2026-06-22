# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def _write_executable(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


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


def test_outputd_failure_reconcile_skips_non_retrying_stops(tmp_path: Path) -> None:
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

    assert not log.exists()
    assert "event=outputd.failure_reconcile.skip" in result.stderr
    assert "reason=non_retrying_stop" in result.stderr
