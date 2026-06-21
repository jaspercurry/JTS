# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the operator-only Wi-Fi scan repair experiment script."""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "experiments" / "wifi-scan-repair.py"


def load_script():
    spec = importlib.util.spec_from_file_location(
        "wifi_scan_repair_experiment",
        SCRIPT,
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_parse_nmcli_wifi_list_flags_single_current_network():
    mod = load_script()
    networks = mod.parse_nmcli_wifi_list(
        "*:Home:81:64:WPA2\n"
        ":Guest:42:6:\n"
        ":hidden::1:WPA2\n"
    )

    assert networks[0] == {
        "ssid": "Home",
        "inUse": True,
        "signal": 81,
        "channel": "64",
        "security": "WPA2",
    }
    assert networks[1]["security"] == "--"


def test_bounce_requires_physical_access_without_ethernet(monkeypatch):
    mod = load_script()
    monkeypatch.setattr(mod, "active_wifi_profile", lambda iface: "Home")
    monkeypatch.setattr(mod, "ethernet_connected", lambda: False)

    args = argparse.Namespace(
        iface="wlan0",
        i_have_physical_access=False,
        rollback_delay=75,
        dry_run=True,
    )

    with pytest.raises(RuntimeError, match="no Ethernet fallback"):
        mod.run_bounce(args)


def test_bounce_dry_run_reports_rollback_timer(monkeypatch):
    mod = load_script()
    monkeypatch.setattr(mod, "active_wifi_profile", lambda iface: "Home")
    monkeypatch.setattr(mod, "ethernet_connected", lambda: False)
    monkeypatch.setattr(
        mod,
        "scan_probe",
        lambda iface: {"iface": iface, "suppressed": True},
    )

    args = argparse.Namespace(
        iface="wlan0",
        i_have_physical_access=True,
        rollback_delay=75,
        dry_run=True,
    )
    result = mod.run_bounce(args)

    assert result["dryRun"] is True
    assert result["profile"] == "Home"
    assert result["hasEthernet"] is False
    assert result["rollbackTimerCommand"][:3] == [
        "systemd-run",
        "--unit=jasper-wifi-bounce-rollback",
        "--on-active=75",
    ]
    assert result["rollbackTimerCommand"][-4:] == [
        "20", "connection", "up", "Home",
    ]
