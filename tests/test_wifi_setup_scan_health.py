# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for scan-health reporting in jasper.web.wifi_setup."""
from __future__ import annotations

import json
import subprocess
from unittest.mock import patch


def _mock_proc(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["nmcli"], returncode=returncode,
        stdout=stdout, stderr=stderr,
    )


def _scripted_nmcli(steps):
    steps_iter = iter(steps)

    def side_effect(cmd, *args, **kwargs):
        try:
            return next(steps_iter)
        except StopIteration:
            return _mock_proc()

    return side_effect


def test_scan_repair_timeout_env_parse_is_fail_soft(monkeypatch):
    import jasper.web.wifi_setup as wifi_setup

    monkeypatch.setenv("JASPER_WIFI_SCAN_REPAIR_ROOT_TIMEOUT", "nope")

    assert wifi_setup._env_float("JASPER_WIFI_SCAN_REPAIR_ROOT_TIMEOUT", 20.0) == 20.0


def test_scan_report_happy_path_deduplicates_and_skips_hidden(monkeypatch):
    import jasper.web.wifi_setup as wifi_setup

    list_stdout = "\n".join([
        "*:ap1:Home:Infra:6:130 Mbit/s:60:WPA2",
        ":ap2:Home:Infra:6:130 Mbit/s:80:WPA2",
        ":ap3:Guest:Infra:11:65 Mbit/s:55:",
        ":ap4::Infra:1:65 Mbit/s:90:WPA2",
    ])
    monkeypatch.setattr(wifi_setup.time, "sleep", lambda *_args: None)
    with patch.object(
        wifi_setup, "_run_nmcli",
        side_effect=_scripted_nmcli([
            _mock_proc(),  # rescan
            _mock_proc(stdout=list_stdout),  # list
        ]),
    ), patch.object(
        wifi_setup, "_recent_kernel_scan_suppressed", return_value=False,
    ):
        report = wifi_setup.scan_networks_report()

    assert report["scan"]["degraded"] is False
    assert report["scan"]["suspect"] is False
    assert [n["ssid"] for n in report["networks"]] == ["Guest"]
    assert report["networks"][0]["security"] == "Open"
    assert report["scan"]["debug"]["rawNetworkCount"] == 2
    assert report["scan"]["debug"]["filteredCurrentCount"] == 1


def test_scan_report_marks_driver_suppression_from_nmcli_error(monkeypatch):
    import jasper.web.wifi_setup as wifi_setup

    monkeypatch.setattr(wifi_setup.time, "sleep", lambda *_args: None)
    monkeypatch.setattr(wifi_setup.os, "geteuid", lambda: 0)
    with patch.object(
        wifi_setup, "_run_nmcli",
        side_effect=_scripted_nmcli([
            _mock_proc(),
            _mock_proc(
                returncode=1,
                stderr="Error: Resource temporarily unavailable (-11)",
            ),
        ]),
    ), patch.object(
        wifi_setup, "_recent_kernel_scan_suppressed", return_value=False,
    ), patch.object(
        wifi_setup.wifi_scan_repair,
        "maybe_repair_scan_suppression",
        return_value=wifi_setup.wifi_scan_repair.RepairResult(
            iface="wlan0",
            attempted=False,
            reason="driver_unknown",
        ),
    ):
        report = wifi_setup.scan_networks_report()

    assert report["networks"] == []
    assert report["scan"]["degraded"] is True
    assert report["scan"]["reason"] == "driver_scan_suppressed"
    assert report["scan"]["debug"]["listReturncode"] == 1
    assert report["scan"]["repair"]["reason"] == "driver_unknown"


def test_scan_report_marks_driver_suppression_from_kernel_log(monkeypatch):
    import jasper.web.wifi_setup as wifi_setup

    list_stdout = "*:ap1:Home:Infra:64:270 Mbit/s:84:WPA2"
    monkeypatch.setattr(wifi_setup.time, "sleep", lambda *_args: None)
    monkeypatch.setattr(wifi_setup.os, "geteuid", lambda: 0)
    with patch.object(
        wifi_setup, "_run_nmcli",
        side_effect=_scripted_nmcli([
            _mock_proc(),
            _mock_proc(stdout=list_stdout),
        ]),
    ), patch.object(
        wifi_setup, "_recent_kernel_scan_suppressed", return_value=True,
    ), patch.object(
        wifi_setup.wifi_scan_repair,
        "maybe_repair_scan_suppression",
        return_value=wifi_setup.wifi_scan_repair.RepairResult(
            iface="wlan0",
            attempted=False,
            reason="cooldown",
            cooldown_remaining=30.0,
        ),
    ):
        report = wifi_setup.scan_networks_report()

    assert report["networks"] == []
    assert report["scan"]["degraded"] is True
    assert report["scan"]["reason"] == "driver_scan_suppressed"
    assert report["scan"]["debug"]["onlyCurrentNetwork"] is True
    assert report["scan"]["debug"]["rawNetworkCount"] == 1
    assert report["scan"]["debug"]["filteredCurrentCount"] == 1


def test_scan_report_single_current_network_is_suspect_not_degraded(
    monkeypatch,
):
    import jasper.web.wifi_setup as wifi_setup

    list_stdout = "*:ap1:Home:Infra:64:270 Mbit/s:84:WPA2"
    monkeypatch.setattr(wifi_setup.time, "sleep", lambda *_args: None)
    with patch.object(
        wifi_setup, "_run_nmcli",
        side_effect=_scripted_nmcli([
            _mock_proc(),
            _mock_proc(stdout=list_stdout),
        ]),
    ), patch.object(
        wifi_setup, "_recent_kernel_scan_suppressed", return_value=False,
    ):
        report = wifi_setup.scan_networks_report()

    assert report["scan"]["degraded"] is False
    assert report["scan"]["suspect"] is True
    assert report["scan"]["reason"] is None
    assert report["networks"] == []


def test_scan_report_tolerates_unavailable_kernel_log_probe(monkeypatch):
    import jasper.web.wifi_setup as wifi_setup

    list_stdout = ":ap1:Home:Infra:6:130 Mbit/s:75:WPA2"
    monkeypatch.setattr(wifi_setup.time, "sleep", lambda *_args: None)
    with patch.object(
        wifi_setup, "_run_nmcli",
        side_effect=_scripted_nmcli([
            _mock_proc(),
            _mock_proc(stdout=list_stdout),
        ]),
    ), patch.object(
        wifi_setup, "_recent_kernel_scan_suppressed", return_value=None,
    ):
        report = wifi_setup.scan_networks_report()

    assert report["scan"]["degraded"] is False
    assert report["scan"]["debug"]["recentSuppressionLog"] is None


def test_scan_report_repairs_driver_suppression_and_retries(monkeypatch):
    import jasper.web.wifi_setup as wifi_setup

    first_list = "*:ap1:Home:Infra:64:270 Mbit/s:84:WPA2"
    healed_list = "\n".join([
        "*:ap1:Home:Infra:64:270 Mbit/s:84:WPA2",
        ":ap2:Guest:Infra:6:130 Mbit/s:70:WPA2",
    ])
    monkeypatch.setattr(wifi_setup.time, "sleep", lambda *_args: None)
    monkeypatch.setattr(wifi_setup.os, "geteuid", lambda: 0)
    with patch.object(
        wifi_setup, "_run_nmcli",
        side_effect=_scripted_nmcli([
            _mock_proc(),
            _mock_proc(stdout=first_list),
            _mock_proc(),
            _mock_proc(stdout=healed_list),
        ]),
    ), patch.object(
        wifi_setup,
        "_recent_kernel_scan_suppressed",
        side_effect=[True, False],
    ), patch.object(
        wifi_setup.wifi_scan_repair,
        "maybe_repair_scan_suppression",
        return_value=wifi_setup.wifi_scan_repair.RepairResult(
            iface="wlan0",
            attempted=True,
            reason="attempted",
            ack=True,
        ),
    ):
        report = wifi_setup.scan_networks_report()

    assert report["scan"]["degraded"] is False
    assert report["scan"]["repair"]["attempted"] is True
    assert report["scan"]["repair"]["ack"] is True
    assert [n["ssid"] for n in report["networks"]] == ["Guest"]
    assert report["scan"]["debug"]["rawNetworkCount"] == 2


def test_scan_report_nonroot_starts_root_repair_helper_and_retries(
    monkeypatch,
    tmp_path,
):
    import jasper.web.wifi_setup as wifi_setup

    first_list = "*:ap1:Home:Infra:64:270 Mbit/s:84:WPA2"
    healed_list = "\n".join([
        "*:ap1:Home:Infra:64:270 Mbit/s:84:WPA2",
        ":ap2:Guest:Infra:6:130 Mbit/s:70:WPA2",
    ])
    state_path = tmp_path / "wifi_scan_repair.json"
    calls = []

    def fake_manage_units(*units, **kwargs):
        calls.append((units, kwargs))
        state_path.write_text(json.dumps({
            "iface": "wlan0",
            "lastAttemptAt": 100.0,
            "lastAck": True,
            "lastReason": "attempted",
            "nextAllowedAt": 160.0,
        }))
        return {"ok": True, "action": "start", "rc": 0}

    monkeypatch.setattr(wifi_setup.time, "sleep", lambda *_args: None)
    monkeypatch.setattr(wifi_setup.time, "time", lambda: 100.0)
    monkeypatch.setattr(wifi_setup.os, "geteuid", lambda: 999)
    monkeypatch.setattr(
        wifi_setup.wifi_scan_repair, "DEFAULT_STATE_PATH", state_path,
    )
    monkeypatch.setattr(wifi_setup, "manage_units", fake_manage_units)
    with patch.object(
        wifi_setup, "_run_nmcli",
        side_effect=_scripted_nmcli([
            _mock_proc(),
            _mock_proc(stdout=first_list),
            _mock_proc(),
            _mock_proc(stdout=healed_list),
        ]),
    ), patch.object(
        wifi_setup,
        "_recent_kernel_scan_suppressed",
        side_effect=[True, False],
    ):
        report = wifi_setup.scan_networks_report()

    assert calls == [(
        ("jasper-wifi-scan-repair.service",),
        {
            "verb": "start",
            "reason": "wifi-scan-suppressed",
            "no_block": False,
            "timeout": 20.0,
        },
    )]
    assert report["scan"]["degraded"] is False
    assert report["scan"]["repair"]["attempted"] is True
    assert report["scan"]["repair"]["ack"] is True
    assert [n["ssid"] for n in report["networks"]] == ["Guest"]
