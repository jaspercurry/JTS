# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import configparser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "deploy" / "systemd"
INSTALL = ROOT / "deploy" / "install.sh"


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

    assert unit["Socket"]["ListenStream"] == "0.0.0.0:5005"
    assert unit["Socket"]["Accept"] == "no"
    assert unit["Socket"]["Service"] == "camillagui-proxy.service"
    assert unit["Socket"]["TriggerLimitIntervalSec"] == "10s"
    assert unit["Socket"]["TriggerLimitBurst"] == "100"
    assert unit["Install"]["WantedBy"] == "sockets.target"


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


def test_install_enables_only_the_camillagui_socket():
    install = INSTALL.read_text()

    for name in (
        "camillagui.service",
        "camillagui-proxy.service",
        "camillagui.socket",
    ):
        assert f'deploy/systemd/{name}"' in install
    assert "systemctl disable camillagui.service" in install
    assert "systemctl enable --now camillagui.socket" in install
