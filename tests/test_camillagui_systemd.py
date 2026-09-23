# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import configparser
from pathlib import Path

from jasper.cli.doctor.web import CAMILLAGUI_PORT

ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "deploy" / "systemd"


def _unit(name: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(
        interpolation=None,
        strict=False,
    )
    parser.optionxform = str
    parser.read(SYSTEMD / name)
    return parser


def test_camillagui_backend_is_dependency_activated_and_idle_stopped():
    unit = _unit("camillagui.service")

    assert unit["Unit"]["StopWhenUnneeded"] == "yes"
    assert "StopWhenUnneeded" not in unit["Service"]
    assert unit["Unit"]["Wants"] == "jasper-camilla.service"
    assert unit["Service"]["Restart"] == "on-failure"
    assert "127.0.0.1/5006" in unit["Service"]["ExecStartPost"]
    assert "WantedBy" not in unit["Install"]


def test_camillagui_socket_activates_the_bounded_proxy():
    unit = _unit("camillagui.socket")

    # #2319: loopback-only. camillagui backs a root process with
    # ReadWritePaths=/etc/camilladsp that can author and live-apply
    # CamillaDSP configs naming any device — 0.0.0.0 made that
    # unauthenticated surface reachable from any device on the LAN.
    assert unit["Socket"]["ListenStream"] == "127.0.0.1:5005"
    assert unit["Socket"]["Accept"] == "no"
    assert unit["Socket"]["Service"] == "camillagui-proxy.service"
    assert unit["Socket"]["TriggerLimitIntervalSec"] == "10s"
    assert unit["Socket"]["TriggerLimitBurst"] == "100"
    assert unit["Install"]["WantedBy"] == "sockets.target"


def test_doctor_camillagui_port_constant_mirrors_the_unit():
    """jasper.cli.doctor.web.CAMILLAGUI_PORT is a second writer of the port
    the shipped unit binds. Without this guard, a port move would update
    the unit (caught by the ListenStream= assertion above) and the suite
    would go green while the doctor's constant stayed stale — the doctor
    would keep probing the OLD port, and a wide-open listener on the NEW
    port would read "OK — not currently listening" instead of warning.
    Precedent: tests/test_env_load_mirrors_unit.py."""
    unit = _unit("camillagui.socket")
    unit_port = int(unit["Socket"]["ListenStream"].rsplit(":", 1)[1])
    assert CAMILLAGUI_PORT == unit_port


def test_camillagui_proxy_owns_backend_lifetime_and_has_no_restart_loop():
    unit = _unit("camillagui-proxy.service")

    requires = unit["Unit"]["Requires"].split()
    assert requires == ["camillagui.socket", "camillagui.service"]
    assert unit["Service"]["Type"] == "notify"
    assert unit["Service"]["ExecStart"] == (
        "/lib/systemd/systemd-socket-proxyd "
        "--exit-idle-time=600 127.0.0.1:5006"
    )
    assert unit["Service"]["Restart"] == "no"
