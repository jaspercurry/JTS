# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pin the jasper-camilla-crossover.service (camilla#2) unit invariants.

camilla#2 is the endpoint-crossover CamillaDSP instance on an active
leader (docs/HANDOFF-distributed-active.md "Stage B"). It coexists with
the always-on camilla#1 and ships INERT — installed but not enabled,
not yet reconciler-gated. The load-bearing safety invariants this file
moats against:
  - **NO StartLimitAction=reboot.** camilla#1 owns the always-on
    recovery/forensics path; camilla#2 is a secondary, reconciler-gated
    instance whose crash must fail CLOSED to silence, NEVER reboot the
    household speaker.
  - **A LIGHTER OOM posture than camilla#1.** Under RAM pressure the kernel
    must reclaim camilla#2 before the always-on camilla#1 (less-negative
    OOMScoreAdjust).
  - **Distinct port (1235) + statefile (crossover-statefile.yml)** so it
    never collides with camilla#1 (:1234, outputd-statefile.yml).
  - **No positional CONFIGFILE** (the v4 statefile-clobber trap), and the
    crossover guard wired as ExecStartPre with the `-` fail-open prefix.
  - **Installed but NOT boot-enabled** by install.sh.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UNIT_PATH = ROOT / "deploy" / "systemd" / "jasper-camilla-crossover.service"
CAMILLA1_UNIT = ROOT / "deploy" / "systemd" / "jasper-camilla.service"
INSTALL_LIB = ROOT / "deploy" / "lib" / "install" / "systemd-units.sh"
INSTALL_SH = ROOT / "deploy" / "install.sh"


def _exec_start_last_line(body: str) -> str:
    in_exec = False
    last_line = ""
    for raw in body.splitlines():
        stripped = raw.strip()
        if stripped.startswith("#") or not stripped:
            continue
        if stripped.startswith("ExecStart="):
            in_exec = True
            last_line = stripped
            continue
        if in_exec:
            last_line = stripped
            if not stripped.endswith("\\"):
                break
    return last_line


def test_unit_uses_port_1235_and_crossover_statefile():
    body = UNIT_PATH.read_text()
    # Check the ExecStart directive lines (not comments) so a comment that
    # mentions camilla#1's port/statefile for contrast doesn't trip this.
    exec_lines = "\n".join(
        ln for ln in body.splitlines()
        if not ln.lstrip().startswith("#")
    )
    assert "-p 1235" in exec_lines
    assert "/var/lib/camilladsp/crossover-statefile.yml" in exec_lines
    # Must not reuse camilla#1's port or statefile in the launch line.
    assert "-p 1234" not in exec_lines
    assert "outputd-statefile.yml" not in exec_lines


def test_unit_has_no_positional_configfile():
    """Same CamillaDSP-v4 statefile-clobber trap as camilla#1: a positional
    config wins on startup AND overwrites the statefile every start. Pin the
    ExecStart's last line to the --statefile arg."""
    last_line = _exec_start_last_line(UNIT_PATH.read_text())
    assert last_line, "ExecStart not found"
    assert "--statefile" in last_line
    assert ".yml" not in last_line.replace("crossover-statefile.yml", "")


def test_unit_never_reboots_the_box():
    """THE critical safety invariant: camilla#2 must NOT carry
    StartLimitAction=reboot. A crash fails closed to silence; only the
    always-on camilla#1 owns a recovery handler."""
    body = UNIT_PATH.read_text()
    directive_lines = [
        ln.strip() for ln in body.splitlines()
        if ln.strip().startswith("StartLimitAction=")
    ]
    assert directive_lines == [], (
        f"camilla#2 must NOT set StartLimitAction (no reboot escalation); "
        f"found {directive_lines!r}"
    )
    # Sanity: camilla#1 DOES recover — confirm the two genuinely differ so
    # this test is meaningful, not vacuously green.
    camilla1 = CAMILLA1_UNIT.read_text()
    assert "StartLimitAction=none" in camilla1
    assert "OnFailure=jasper-camilla-recover.service" in camilla1


def test_unit_keeps_a_startlimit_loop_bound():
    """No reboot, but still a loop-bound so a genuinely broken config does
    not spin forever (it parks failed instead)."""
    body = UNIT_PATH.read_text()
    assert "StartLimitIntervalSec=" in body
    assert "StartLimitBurst=" in body


def test_unit_recovers_but_does_not_use_restart_always():
    """Restart so a crash recovers, but NOT Restart=always: a clean exit is
    an intentional reconciler stop that must not be fought."""
    body = UNIT_PATH.read_text()
    directive_lines = [
        ln.strip() for ln in body.splitlines()
        if ln.strip().startswith("Restart=")
    ]
    assert directive_lines == ["Restart=on-failure"], directive_lines


def test_unit_oom_posture_is_lighter_than_camilla1():
    """Under RAM pressure the kernel must reclaim camilla#2 BEFORE the
    always-on camilla#1: a strictly less-negative OOMScoreAdjust."""
    def _oom(body: str) -> int:
        vals = [
            ln.strip().split("=", 1)[1]
            for ln in body.splitlines()
            if ln.strip().startswith("OOMScoreAdjust=")
        ]
        assert len(vals) == 1, vals
        return int(vals[0])

    crossover = _oom(UNIT_PATH.read_text())
    camilla1 = _oom(CAMILLA1_UNIT.read_text())
    assert camilla1 == -900  # documents the baseline this test compares to
    assert crossover == -500
    assert crossover > camilla1, (
        "camilla#2 must be MORE reclaimable than camilla#1"
    )
    # Still biased to survive over ordinary unbiased (0) work.
    assert crossover < 0


def test_unit_shares_audio_slice_and_quality_knobs():
    body = UNIT_PATH.read_text()
    assert "Slice=jts-audio.slice" in body
    assert "Nice=-10" in body
    assert "IOSchedulingClass=realtime" in body
    assert "LimitRTPRIO=99" in body
    assert "LimitMEMLOCK=infinity" in body


def test_unit_wires_crossover_guard_fail_open():
    body = UNIT_PATH.read_text()
    assert "ExecStartPre=-/usr/local/sbin/jasper-camilla-crossover-guard" in body


def test_install_installs_unit_and_guard_but_does_not_enable():
    """Installed in BOTH the full and streambox unit paths, with the guard to
    /usr/local/sbin, and NEVER `systemctl enable`d (reconciler arms it)."""
    lib = INSTALL_LIB.read_text()
    assert (
        "deploy/systemd/jasper-camilla-crossover.service" in lib
    )
    assert (
        "/usr/local/sbin/jasper-camilla-crossover-guard" in lib
    )
    # Not enabled anywhere in the installer.
    for body in (lib, INSTALL_SH.read_text()):
        assert "enable jasper-camilla-crossover" not in body
        assert "enable --now jasper-camilla-crossover" not in body


def test_install_seeds_crossover_statefile_via_runtime_contract():
    """install.sh seeds crossover-statefile.yml through the same runtime
    contract (driver-domain baseline on a roleful topology, never flat)."""
    body = INSTALL_SH.read_text()
    assert "ensure_crossover_camilla_statefile" in body
    assert "/var/lib/camilladsp/crossover-statefile.yml" in body
    # Reuses the runtime-safe-graph CLI (no hand-rolled flat seed).
    assert "runtime-safe-graph" in body


def test_unit_listed_in_streambox_systemd_analyze_verify():
    """A deploy runs systemd-analyze verify on this unit (streambox path)."""
    lib = INSTALL_LIB.read_text()
    assert '"${SYSTEMD_DIR}/jasper-camilla-crossover.service"' in lib
