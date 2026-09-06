# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The reboot-required marker (issue #2110): one canonical, machine-readable
file that onboard.sh and deploy-to-pi.sh read instead of parsing install.sh's
log prose for "REBOOT REQUIRED". Every writer owns one key and may clear only
its own, so no writer can step on another's reason regardless of call order.

Two writers are covered here: the zram/cgroup migrations in
deploy/lib/install/memory-resilience.sh, and install.sh's
install_snd_aloop_options."""
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


#: What an earlier deploy left in the marker; a run that must not touch the
#: key has to leave exactly this, and a run that clears it must remove it.
SEEDED_SND_ALOOP = "snd_aloop=deferred by an earlier deploy"
SEEDED_OTHER = "zram=resize pending"


# Removal condition: drop this pin when the install parks the audio graph
# before the reload (needs #4123's install-in-progress marker on every box),
# because then the unload succeeds and no reboot is ever deferred.
@pytest.mark.parametrize(
    ("module_loaded", "rmmod_rc", "conf_changed", "expected"),
    [
        # The live box: fanin holds the capture sides, the unload is EBUSY.
        (True, 1, True, "set"),
        # Nothing holds it: remove + add applies the shipped options now.
        (True, 0, True, "cleared"),
        # First install: nothing to unload, the add still has to happen.
        (False, 0, True, "cleared"),
        # Busy, but this deploy shipped no option change — the earlier
        # deploy's pending reboot must survive, and no new one is asked for.
        (True, 1, False, "kept"),
    ],
)
def test_a_busy_snd_aloop_defers_its_options_to_the_marker(
    tmp_path: Path,
    module_loaded: bool,
    rmmod_rc: int,
    conf_changed: bool,
    expected: str,
) -> None:
    marker = tmp_path / "reboot_required"
    marker.write_text(f"{SEEDED_OTHER}\n{SEEDED_SND_ALOOP}\n", encoding="utf-8")
    calls = tmp_path / "calls.log"
    sysfs = tmp_path / "sys_module_snd_aloop"
    if module_loaded:
        sysfs.mkdir()
    r = _run(
        f"""
        install() {{ return 0; }}
        cmp() {{ return {1 if conf_changed else 0}; }}
        rmmod() {{ echo "rmmod $*" >> {shlex.quote(str(calls))}; return {rmmod_rc}; }}
        modprobe() {{ echo "modprobe $*" >> {shlex.quote(str(calls))}; return 0; }}
        install_snd_aloop_options {shlex.quote(str(sysfs))}
        """,
        marker,
        source=INSTALL_SH,
    )
    assert r.returncode == 0, r.stderr
    lines = marker.read_text(encoding="utf-8").splitlines() if marker.exists() else []
    assert SEEDED_OTHER in lines
    snd = [line for line in lines if line.startswith("snd_aloop=")]
    if expected == "cleared":
        assert snd == []
    elif expected == "kept":
        assert snd == [SEEDED_SND_ALOOP]
    else:
        assert snd and snd != [SEEDED_SND_ALOOP]
    logged = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    # The module is always (re)loaded; only a loaded module is unloaded first.
    assert "modprobe snd-aloop" in logged
    assert ("rmmod snd_aloop" in logged) is module_loaded


def test_a_module_that_will_not_load_fails_the_install(tmp_path: Path) -> None:
    """A snd-aloop that cannot be inserted is not a deferrable condition: every
    later audio step needs the card, so the installer stops instead of marking
    a reboot and carrying on."""
    marker = tmp_path / "reboot_required"
    r = _run(
        f"""
        install() {{ return 0; }}
        cmp() {{ return 1; }}
        rmmod() {{ return 0; }}
        modprobe() {{ return 1; }}
        install_snd_aloop_options {shlex.quote(str(tmp_path / "absent"))}
        """,
        marker,
        source=INSTALL_SH,
    )
    assert r.returncode != 0
    assert not marker.exists()
