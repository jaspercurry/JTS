# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Deployment contracts for the optional enhanced-AEC background job."""
import os
from pathlib import Path
import shlex
import subprocess


ROOT = Path(__file__).parent.parent
SERVICE = ROOT / "deploy/systemd/jasper-enhanced-aec-install.service"
RECONCILE_PATH = ROOT / "deploy/systemd/jasper-enhanced-aec-reconcile.path"
CONTAINED_BUILD = ROOT / "deploy/bin/jasper-contained-build"


def test_installer_is_a_bounded_low_priority_root_oneshot():
    text = SERVICE.read_text(encoding="utf-8")
    assert "Type=oneshot" in text
    assert "User=root" in text
    assert "TimeoutStartSec=30min" in text
    assert "TimeoutStopSec=10s" in text
    assert "KillMode=control-group" in text
    assert "Environment=JASPER_BUILD_SANDBOX=0" in text
    assert "ConditionPathExists=/var/lib/jasper/enhanced-aec-intent.json" in text
    assert "OOMScoreAdjust=900" in text
    assert "CPUWeight=20" in text
    assert "IOWeight=20" in text
    assert "Restart=" not in text
    assert "WantedBy=multi-user.target" in text


def test_successful_manifest_change_retries_without_a_path_exists_loop():
    text = RECONCILE_PATH.read_text(encoding="utf-8")
    assert "PathChanged=/var/lib/jasper/build.txt" in text
    assert "Unit=jasper-enhanced-aec-install.service" in text
    assert "PathExists=" not in text
    assert "WantedBy=multi-user.target" in text


def _testable_runtime_builder(tmp_path: Path) -> Path:
    """Point the installed helper at this checkout's canonical policy."""

    policy = ROOT / "deploy/lib/install/build-sandbox.sh"
    text = CONTAINED_BUILD.read_text(encoding="utf-8").replace(
        "source /usr/local/lib/jasper/install/build-sandbox.sh",
        f"source {shlex.quote(str(policy))}",
    )
    helper = tmp_path / "jasper-contained-build"
    helper.write_text(text, encoding="utf-8")
    helper.chmod(0o755)
    return helper


def test_runtime_builder_gives_each_transient_scope_an_independent_cap(
    tmp_path: Path,
):
    helper = _testable_runtime_builder(tmp_path)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    systemd_run = bindir / "systemd-run"
    systemd_run.write_text(
        '#!/usr/bin/env bash\n'
        'for arg in "$@"; do printf "ARG=%s\\n" "$arg"; done\n',
        encoding="utf-8",
    )
    systemd_run.chmod(0o755)
    env = os.environ.copy()
    env.update({
        "PATH": f"{bindir}:{env['PATH']}",
        "JASPER_BUILD_SANDBOX": "1",
        "JASPER_BUILD_SWAP": "off",
    })

    result = subprocess.run(
        [str(helper), "enhanced-aec-test", "--", "/usr/bin/true"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "ARG=--property=RuntimeMaxSec=14m" in result.stdout


def test_runtime_builder_direct_fallback_caps_the_process_group(
    tmp_path: Path,
):
    helper = _testable_runtime_builder(tmp_path)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    timeout_stub = bindir / "timeout"
    timeout_stub.write_text(
        '#!/usr/bin/env bash\n'
        'for arg in "$@"; do printf "ARG=%s\\n" "$arg"; done\n',
        encoding="utf-8",
    )
    timeout_stub.chmod(0o755)
    env = os.environ.copy()
    env.update({
        "PATH": f"{bindir}:{env['PATH']}",
        "JASPER_BUILD_SANDBOX": "0",
        "JASPER_BUILD_SWAP": "off",
    })

    result = subprocess.run(
        [str(helper), "enhanced-aec-test", "--", "/usr/bin/true"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "ARG=--signal=TERM" in result.stdout
    assert "ARG=--kill-after=20s" in result.stdout
    assert "ARG=14m" in result.stdout
    assert "ARG=--foreground" not in result.stdout
