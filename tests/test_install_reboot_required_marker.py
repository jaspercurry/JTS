# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The two /run/jasper-install markers deploy/lib/install/memory-resilience.sh
writes.

reboot_required (issue #2110): one canonical, machine-readable file that
onboard.sh and deploy-to-pi.sh read instead of parsing install.sh's log prose
for "REBOOT REQUIRED". Each migration owns one key in the file and clears it
on every run before possibly re-setting it, so two migrations can't step on
each other's reason regardless of call order.

in_progress (issue #4123): present for the window in which the installer is
mutating /opt/jasper, so asynchronously activated units skip the pass rather
than import a half-synced tree."""
from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
MEMORY_RESILIENCE_SH = REPO_ROOT / "deploy" / "lib" / "install" / "memory-resilience.sh"
BUILD_SANDBOX_SH = REPO_ROOT / "deploy" / "lib" / "install" / "build-sandbox.sh"


def _run(script: str, marker: Path) -> subprocess.CompletedProcess[str]:
    full = f"set -euo pipefail; source {shlex.quote(str(MEMORY_RESILIENCE_SH))} >/dev/null && {script}"
    env = dict(os.environ)
    env["JTS_REBOOT_REQUIRED_MARKER"] = str(marker)
    return subprocess.run(
        ["bash", "-c", full],
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )


def test_marker_absent_when_nothing_pending(tmp_path: Path) -> None:
    marker = tmp_path / "reboot_required"
    r = _run("_print_reboot_required_marker", marker)
    assert r.returncode == 0, r.stderr
    assert r.stdout == ""
    assert not marker.exists()


def test_marker_records_and_prints_a_reason(tmp_path: Path) -> None:
    marker = tmp_path / "reboot_required"
    r = _run(
        '_set_reboot_required_reason zram "resize pending" && _print_reboot_required_marker',
        marker,
    )
    assert r.returncode == 0, r.stderr
    assert marker.read_text(encoding="utf-8") == "zram=resize pending\n"
    assert "REBOOT REQUIRED" in r.stdout
    assert "resize pending" in r.stdout


def test_clearing_the_only_reason_removes_the_marker_file(tmp_path: Path) -> None:
    marker = tmp_path / "reboot_required"
    r = _run(
        '_set_reboot_required_reason zram "resize pending" '
        '&& _set_reboot_required_reason zram "" '
        '&& _print_reboot_required_marker',
        marker,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout == ""
    assert not marker.exists()


def test_two_migrations_dont_clobber_each_others_reason(tmp_path: Path) -> None:
    marker = tmp_path / "reboot_required"
    r = _run(
        '_set_reboot_required_reason cgroup_memory "cmdline updated" '
        '&& _set_reboot_required_reason zram "resize pending" '
        '&& _set_reboot_required_reason cgroup_memory "" '
        '&& _print_reboot_required_marker',
        marker,
    )
    assert r.returncode == 0, r.stderr
    assert marker.read_text(encoding="utf-8") == "zram=resize pending\n"
    assert "resize pending" in r.stdout
    assert "cmdline updated" not in r.stdout


def test_install_in_progress_marker_survives_the_window_and_the_exit_trap_clears_it(
    tmp_path: Path,
) -> None:
    """The installer's EXIT trap is the failure path's only chance to clear it:
    a marker left behind would keep every gated reconciler skipped until the
    next reboot flushes /run."""
    marker = tmp_path / "reboot_required"
    in_progress = tmp_path / "in_progress"
    env = dict(os.environ)
    env["JTS_REBOOT_REQUIRED_MARKER"] = str(marker)
    env["JASPER_DEPLOY_SHA_FULL"] = "0123456789abcdef"
    r = subprocess.run(
        ["bash", "-c", "set -euo pipefail; "
         f"source {shlex.quote(str(MEMORY_RESILIENCE_SH))} >/dev/null; "
         f"source {shlex.quote(str(BUILD_SANDBOX_SH))} >/dev/null; "
         "mark_install_in_progress; "
         f"cat {shlex.quote(str(in_progress))}; "
         "install_exit_cleanup"],
        capture_output=True, text=True, timeout=5, env=env,
    )
    assert r.returncode == 0, r.stderr
    assert "sha=0123456789abcdef" in r.stdout
    assert not in_progress.exists()
