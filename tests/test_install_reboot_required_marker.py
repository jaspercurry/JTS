# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The two /run/jasper-install markers memory-resilience.sh writes.

reboot_required (#2110): one canonical, machine-readable file that onboard.sh
and deploy-to-pi.sh read instead of parsing install.sh's log prose for "REBOOT
REQUIRED". Each migration owns one key and clears it on every run before
possibly re-setting it, so two migrations can't step on each other's reason
regardless of call order. install.sh's install_snd_aloop_options writes a key
here too, under the amendment memory-resilience.sh's own header records.

in_progress (#4123): present while the installer mutates /opt/jasper, so
asynchronously activated units skip the pass rather than import a half-synced
tree."""
from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import pytest

from .systemd_unit_helpers import value_for

REPO_ROOT = Path(__file__).parent.parent
MEMORY_RESILIENCE_SH = REPO_ROOT / "deploy" / "lib" / "install" / "memory-resilience.sh"
BUILD_SANDBOX_SH = REPO_ROOT / "deploy" / "lib" / "install" / "build-sandbox.sh"
INSTALL_SH = REPO_ROOT / "deploy" / "install.sh"
GATED_UNIT = REPO_ROOT / "deploy" / "systemd" / "jasper-audio-hardware-reconcile.service"
WIFI_RECOVER = REPO_ROOT / "deploy" / "bin" / "jasper-wifi-recover"


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


def test_all_three_install_marker_defaults_name_the_path_the_units_gate_on() -> None:
    """A unit cannot read the shell seam and neither can jasper-wifi-recover,
    so the three copies of this path have to be kept in step by hand."""
    gate = value_for(GATED_UNIT.read_text(encoding="utf-8"), "ConditionPathExists")
    marker = gate.lstrip("!")

    seam = subprocess.run(
        ["bash", "-c", "set -euo pipefail; "
         f"source {shlex.quote(str(MEMORY_RESILIENCE_SH))} >/dev/null; "
         'printf "%s" "${INSTALL_IN_PROGRESS_MARKER}"'],
        capture_output=True, text=True, timeout=5,
        env={k: v for k, v in os.environ.items() if k != "JTS_REBOOT_REQUIRED_MARKER"},
    )
    assert seam.returncode == 0, seam.stderr
    assert seam.stdout == marker

    # `--help` returns after the assignments, and xtrace reports the value the
    # script really resolved — an execution read, not a grep of its source.
    recover = subprocess.run(
        ["bash", "-x", str(WIFI_RECOVER), "--help"],
        capture_output=True, text=True, timeout=5,
        env={k: v for k, v in os.environ.items()
             if k != "JASPER_INSTALL_IN_PROGRESS_MARKER"},
    )
    assert recover.returncode == 0, recover.stderr
    assert f"INSTALL_MARKER={marker}\n" in recover.stderr


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
