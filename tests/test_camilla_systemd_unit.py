# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pin the jasper-camilla.service systemd unit invariants.

The unit owns the load-bearing CamillaDSP launch — anything that drifts
here can silently break audio (Restart=always, the StartLimit recovery
policy) or silently wipe a user's room correction on reboot (--statefile).

These tests are a defensive moat around regressions like:
  - "we tweaked ExecStart and accidentally dropped --statefile" →
    every reboot loses the loaded config, correction lost
  - "we removed Restart=always to make systemd less aggressive" →
    a clean exit leaves audio dead until manual intervention
    (real 2026-05-07 incident in the unit header comment)
"""
from __future__ import annotations

from pathlib import Path

from tests.reconcile_fixtures import fake_systemctl
from tests.test_audio_hardware_reconcile import APPLE_LISTING, _run_reconcile
from tests.systemd_unit_helpers import (
    assignments_for as _assignments_for,
    value_for as _value_for,
    values_for as _values_for,
)

UNIT_PATH = (
    Path(__file__).resolve().parent.parent
    / "deploy" / "systemd" / "jasper-camilla.service"
)
RECOVER_UNIT_PATH = (
    Path(__file__).resolve().parent.parent
    / "deploy" / "systemd" / "jasper-camilla-recover.service"
)
INSTALL_SH = (
    Path(__file__).resolve().parent.parent / "deploy" / "install.sh"
)


def test_unit_starts_after_outputd_and_fanin_for_pipe_rendezvous():
    body = UNIT_PATH.read_text()

    after = _values_for(body, "After")
    wants = _values_for(body, "Wants")
    assert "jasper-outputd.service" in after
    assert "jasper-fanin.service" in after
    assert "jasper-outputd.service" in wants
    assert "jasper-fanin.service" in wants


def test_unit_elects_rt_below_the_sinks_and_bounds_rttime():
    """Audio-latency foundation G1+G4. CamillaDSP must run SCHED_FIFO so it
    isn't preempted under load (the source of the fan-in short-read/xrun
    storms), but BELOW the two sinks that have to win the CPU when the system
    is starved: jasper-fanin (30) and jasper-outputd (35). Camilla feeds them,
    so the ordering 25 < 30 < 35 must hold — verify it against the sibling
    units, not just a literal. LimitRTPRIO=99 gives the binary's
    audio_thread_priority promotion headroom; LimitRTTIME=200000 (200 ms) bounds
    a runaway RT thread to a SIGXCPU instead of a watchdog reboot (mandatory
    with G1)."""
    unit = UNIT_PATH.read_text()
    assert _value_for(unit, "CPUSchedulingPolicy") == "fifo"
    camilla_prio = int(_value_for(unit, "CPUSchedulingPriority"))
    assert camilla_prio == 25
    assert _value_for(unit, "LimitRTPRIO") == "99"
    assert _value_for(unit, "LimitRTTIME") == "200000"

    systemd_dir = UNIT_PATH.parent
    fanin_prio = int(
        _value_for(
            (systemd_dir / "jasper-fanin.service").read_text(),
            "CPUSchedulingPriority",
        )
    )
    outputd_prio = int(
        _value_for(
            (systemd_dir / "jasper-outputd.service").read_text(),
            "CPUSchedulingPriority",
        )
    )
    assert camilla_prio < fanin_prio < outputd_prio, (
        f"Camilla ({camilla_prio}) must stay below fan-in ({fanin_prio}) and "
        f"outputd ({outputd_prio}) so the sinks win the CPU when starved."
    )


def test_unit_passes_cutover_statefile_to_camilladsp():
    """The outputd topology uses a separate Camilla statefile so
    rollback to pre-outputd code can keep the user's normal correction statefile
    intact."""
    body = UNIT_PATH.read_text()
    assert "--statefile" in body
    assert "/var/lib/camilladsp/outputd-statefile.yml" in body


def test_unit_has_no_positional_configfile():
    """CamillaDSP behavior we hit on first cutover: when both a
    positional CONFIGFILE and --statefile are given, the positional
    WINS on startup AND clobbers the statefile with the positional
    path on every start. So having a config path as a positional arg
    here defeats the entire persistence feature. Fresh installs are
    handled by install.sh seeding the statefile.
    Pin the absence so this doesn't quietly come back."""
    body = UNIT_PATH.read_text()
    # The positional arg would appear on its own line after the
    # other ExecStart args. Verify the ExecStart's last non-comment
    # non-blank line is the --statefile arg, not a CONFIGFILE path.
    in_exec = False
    last_line = None
    for raw in body.splitlines():
        stripped = raw.strip()
        if stripped.startswith("#") or not stripped:
            continue
        if stripped.startswith("ExecStart="):
            in_exec = True
            last_line = stripped
            continue
        if in_exec:
            if stripped.endswith("\\"):
                last_line = stripped
                continue
            # First non-continuation line ends the ExecStart.
            last_line = stripped
            break
    assert last_line is not None
    assert "v1.yml" not in last_line, (
        f"ExecStart ends with a positional config — clobbers statefile. "
        f"Last line: {last_line!r}"
    )
    assert "--statefile" in last_line


# Restart=always (Restart=on-failure ignores clean exits — a real
# 2026-05-07 incident left the speaker silently dead overnight because
# Camilla exited cleanly) is pinned, with the rest of camilla's restart
# policy, by tests/test_systemd_hardening.py's RESTART_POLICY table (R22,
# #4416).


def test_unit_uses_recovery_handler_instead_of_raw_reboot():
    """JTS5's ALSA-busy failure class needs holder forensics and a bounded
    graph restart, not an immediate blind reboot."""
    body = UNIT_PATH.read_text()
    # StartLimitAction=none, StartLimitIntervalSec=60 and StartLimitBurst=5
    # are pinned by RESTART_POLICY (R22, #4416).
    assert _values_for(body, "OnFailure") == ("jasper-camilla-recover.service",)


def test_recovery_unit_points_at_installed_helper():
    body = RECOVER_UNIT_PATH.read_text()
    assert _value_for(body, "Type") == "oneshot"
    assert _assignments_for(body, "ExecStart") == (
        "/usr/local/sbin/jasper-camilla-recover --reason start-limit",
    )
    # The deadline must cover the handler's own pass: bounded captures, one
    # blocking camilla start behind every unit it pulls in, the liveness wait.
    assert _value_for(body, "TimeoutStartSec") == "180"
    assert _value_for(body, "TimeoutStopSec") == "5"


def test_install_sh_repairs_generated_camilla_config_modes_for_non_root_daemons():
    """Stale generated YAML may predate the non-root control/web readers.

    The sudo CLI can read root:root 0600 generated configs, but jasper-control
    and jasper-web read configs/*.yml for /state, /sound, and active-driver
    flows. Repair all generated YAMLs, not just active-speaker baselines.
    """

    body = INSTALL_SH.read_text()
    assert "-name '*.yml'" in body
    assert "-exec chgrp jasper {} +" in body
    assert "-exec chmod 0640 {} +" in body


def test_flat_cutover_is_published_before_the_audio_restart(tmp_path):
    cutover = tmp_path / "camilladsp" / "outputd-cutover.yml"
    systemctl, transcript = fake_systemctl(
        tmp_path, name="witness-systemctl", witness="CUTOVER_WITNESS"
    )
    result = _run_reconcile(
        tmp_path, APPLE_LISTING,
        extra_env={
            "JASPER_SYSTEMCTL": str(systemctl),
            "JASPER_SYSTEMCTL_LOG": str(transcript),
            "CUTOVER_WITNESS": str(cutover),
        },
    )
    assert result.returncode == 0, result.stderr
    restarts = [
        line for line in transcript.read_text().splitlines()
        if "restart" in line.split() and "jasper-outputd.service" in line.split()
    ]
    assert restarts == ["present=1 --no-block restart jasper-outputd.service"]


def test_unit_documents_no_config_recovery_path():
    """The recovery path for "bad correction wedges the speaker" is
    to add --no_config to the ExecStart args. Pin the inline doc so
    it doesn't drift — a stranded operator should be able to read
    the unit and know what to do."""
    body = UNIT_PATH.read_text()
    assert "--no_config" in body
