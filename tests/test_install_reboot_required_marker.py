# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The reboot-required marker deploy/lib/install/memory-resilience.sh writes
(issue #2110): one canonical, machine-readable file that onboard.sh and
deploy-to-pi.sh read instead of parsing install.sh's log prose for
"REBOOT REQUIRED". Each migration owns one key in the file and clears it
on every run before possibly re-setting it, so two migrations can't step
on each other's reason regardless of call order.

install.sh's snd-aloop reload is the third writer: options bind at module
load, so a conf change that cannot unload the busy module is disclosed
through the same marker rather than silently deferred to some later boot."""
from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
MEMORY_RESILIENCE_SH = REPO_ROOT / "deploy" / "lib" / "install" / "memory-resilience.sh"
INSTALL_SH = REPO_ROOT / "deploy" / "install.sh"


def _run(
    script: str, marker: Path, source: Path = MEMORY_RESILIENCE_SH
) -> subprocess.CompletedProcess[str]:
    full = f"set -euo pipefail; source {shlex.quote(str(source))} >/dev/null && {script}"
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


# Removal condition: drop this pin when the install parks the audio graph
# before the reload (needs #4123's install-in-progress marker on every box),
# because then the unload succeeds and no reboot is ever deferred.
@pytest.mark.parametrize(
    ("rmmod_rc", "module_loaded", "conf_changed", "expect_key", "expect_modprobe"),
    [
        # The live box: fanin holds the capture sides, the unload is EBUSY.
        (1, True, True, True, False),
        # Nothing holds it: remove + add applies the shipped options now.
        (0, True, True, False, True),
        # First install: nothing to unload, the add still has to happen.
        (1, False, True, False, True),
        # Busy, but the conf did not change — nothing is pending a reboot.
        (1, True, False, False, False),
    ],
)
def test_busy_snd_aloop_reload_defers_to_the_marker(
    tmp_path: Path,
    rmmod_rc: int,
    module_loaded: bool,
    conf_changed: bool,
    expect_key: bool,
    expect_modprobe: bool,
) -> None:
    marker = tmp_path / "reboot_required"
    calls = tmp_path / "calls.log"
    lsmod_out = 'echo "snd_aloop 32768 3 -"' if module_loaded else "true"
    r = _run(
        f"""
        install() {{ return 0; }}
        cmp() {{ return {1 if conf_changed else 0}; }}
        lsmod() {{ {lsmod_out}; }}
        rmmod() {{ echo "rmmod $*" >> {shlex.quote(str(calls))}; return {rmmod_rc}; }}
        modprobe() {{ echo "modprobe $*" >> {shlex.quote(str(calls))}; return 0; }}
        install_snd_aloop_options
        """,
        marker,
        source=INSTALL_SH,
    )
    assert r.returncode == 0, r.stderr
    keys = marker.read_text(encoding="utf-8").splitlines() if marker.exists() else []
    assert any(k.startswith("snd_aloop=") for k in keys) is expect_key
    logged = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    assert ("modprobe snd-aloop" in logged) is expect_modprobe
