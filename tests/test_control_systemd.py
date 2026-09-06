# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Lock down the jasper-control.service systemd unit shape.

jasper-control persists small state files under /var/lib/jasper — the
wizard env files it owns (aec_mode.env, wake_model.env, debug.env),
speaker_volume.json, and the T5.2 SystemSupervisor's reboot rate-limit
at /var/lib/jasper/system_supervisor_reboot.json. That last one is
load-bearing: the persisted timestamp is what keeps a *permanent*
userspace wedge from reboot-looping forever (see
jasper/control/system_supervisor.py).

Today those writes work only because `ProtectSystem=full` leaves /var
writable. A future tightening to `ProtectSystem=strict` would silently
make /var read-only and regress the reboot-loop guard. The explicit
`ReadWritePaths=/var/lib/jasper` pins the contract; this test catches a
config edit that drops it. Mirrors tests/test_fanin_systemd.py.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from tests.systemd_unit_helpers import (
    assignments_for as _assignments_for,
    value_for as _value_for,
    values_for as _values_for,
)


REPO = Path(__file__).resolve().parents[1]
UNIT_PATH = REPO / "deploy" / "systemd" / "jasper-control.service"
GROUPING_TRAILING_SERVICE_PATH = (
    REPO / "deploy" / "systemd" / "jasper-grouping-reconcile-trailing.service"
)
GROUPING_TRAILING_HELPER_PATH = (
    REPO / "deploy" / "bin" / "jasper-grouping-reconcile-trailing"
)
GROUPING_KICK_HELPER_PATH = (
    REPO / "deploy" / "bin" / "jasper-grouping-reconcile-kick"
)
GROUPING_RECONCILE_SERVICE_PATH = (
    REPO / "deploy" / "systemd" / "jasper-grouping-reconcile.service"
)


def _read_unit() -> str:
    return UNIT_PATH.read_text()


def test_unit_file_exists():
    assert UNIT_PATH.exists(), (
        f"jasper-control.service missing at {UNIT_PATH}."
    )


def test_grouping_reconcile_trailing_service_runs_fixed_helper():
    from jasper.control import server as control_server

    unit = GROUPING_TRAILING_SERVICE_PATH.read_text()
    assert (
        _assignments_for(unit, "ExecStart")
        == ("/usr/local/sbin/jasper-grouping-reconcile-trailing",)
    )
    assert _values_for(unit, "Environment") == (
        "JASPER_GROUPING_TRAILING_DELAY_FILE="
        f"{control_server._GROUPING_RECONCILE_TRAILING_DELAY_FILE}",
    )
    assert _value_for(unit, "NoNewPrivileges") == "true"
    assert _values_for(unit, "CapabilityBoundingSet") == ()


def test_install_installs_grouping_reconcile_trailing_helper():
    units_sh = (REPO / "deploy/lib/install/systemd-units.sh").read_text()
    assert units_sh.count("jasper-grouping-reconcile-trailing.service") >= 2
    assert units_sh.count("jasper-grouping-reconcile-trailing\"") >= 1
    assert units_sh.count("jasper-grouping-reconcile-kick\"") >= 1


def test_grouping_reconcile_trailing_helper_uses_decimal_delay(tmp_path):
    delay_file = tmp_path / "delay"
    delay_file.write_text("008\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    sleep_log = tmp_path / "sleep.log"
    systemctl_log = tmp_path / "systemctl.log"

    sleep = bin_dir / "sleep"
    sleep.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$1\" > {sleep_log}\n")
    sleep.chmod(0o755)
    kick = bin_dir / "jasper-grouping-reconcile-kick"
    kick.write_text(f"#!/bin/sh\nprintf '%s\\n' kick > {systemctl_log}\n")
    kick.chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "JASPER_GROUPING_TRAILING_DELAY_FILE": str(delay_file),
        "JASPER_GROUPING_KICK_HELPER": str(kick),
    }

    subprocess.run([str(GROUPING_TRAILING_HELPER_PATH)], env=env, check=True)

    assert sleep_log.read_text() == "8\n"
    assert systemctl_log.read_text() == "kick\n"


def test_grouping_reconcile_kick_joins_then_queues_fresh_pass(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    systemctl_log = tmp_path / "systemctl.log"
    systemctl = bin_dir / "systemctl"
    systemctl.write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {systemctl_log}\n",
    )
    systemctl.chmod(0o755)
    timeout = bin_dir / "timeout"
    timeout.write_text("#!/bin/sh\nshift\nexec \"$@\"\n")
    timeout.chmod(0o755)

    subprocess.run(
        [str(GROUPING_KICK_HELPER_PATH)],
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        check=True,
    )

    assert systemctl_log.read_text().splitlines() == [
        "reset-failed jasper-grouping-reconcile.service",
        "start jasper-grouping-reconcile.service",
        "--no-block start jasper-grouping-reconcile.service",
    ]


def test_grouping_kick_drain_outlasts_legal_reconcile_activation():
    helper = GROUPING_KICK_HELPER_PATH.read_text()
    unit = GROUPING_RECONCILE_SERVICE_PATH.read_text()
    drain_match = re.search(r'^drain_timeout="(\d+)s"$', helper, re.MULTILINE)
    unit_timeout = _value_for(unit, "TimeoutStartSec")

    assert drain_match is not None
    assert unit_timeout is not None
    assert int(drain_match.group(1)) > int(unit_timeout.rstrip("s"))


def test_readwritepaths_pins_control_write_contracts():
    """The state-write contract must be explicit, not incidental.

    Without this line the /var/lib/jasper writes (including the
    supervisor's reboot rate-limit) survive only because
    ProtectSystem=full happens to leave /var writable — a
    ProtectSystem=strict edit would silently break persistence and
    regress to the reboot-loop the rate-limit exists to prevent."""
    unit = _read_unit()
    paths = _values_for(unit, "ReadWritePaths")
    assert paths, (
        "jasper-control.service must declare ReadWritePaths to pin its "
        "state and peering advert write contracts."
    )
    assert "/var/lib/jasper" in paths, (
        "ReadWritePaths must include /var/lib/jasper; the T5.2 reboot "
        "rate-limit at /var/lib/jasper/system_supervisor_reboot.json depends "
        f"on it. Got {paths!r}"
    )
    assert "/etc/avahi/services" in paths, (
        "ReadWritePaths must include /etc/avahi/services; wake-response "
        "peering renders /etc/avahi/services/jasper-peer.service from inside "
        f"jasper-control under ProtectSystem=full. Got {paths!r}"
    )


def test_unit_caps_tasks_without_memorymax_kill_boundary():
    """Control sheds overload in-process; systemd caps runaway task growth.

    Do not use MemoryMax here: jasper-control is the protected recovery
    surface, so a cgroup-local OOM kill would remove the dashboard/control
    plane exactly when the household needs it.
    """
    unit = _read_unit()
    assert _value_for(unit, "TasksMax") == "256"
    assert _value_for(unit, "MemoryMax") is None
