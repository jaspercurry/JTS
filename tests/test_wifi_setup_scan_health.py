"""Tests for scan-health reporting in jasper.web.wifi_setup."""
from __future__ import annotations

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
    assert [n["ssid"] for n in report["networks"]] == ["Home", "Guest"]
    assert report["networks"][0]["signal"] == 80
    assert report["networks"][1]["security"] == "Open"


def test_scan_report_marks_driver_suppression_from_nmcli_error(monkeypatch):
    import jasper.web.wifi_setup as wifi_setup

    monkeypatch.setattr(wifi_setup.time, "sleep", lambda *_args: None)
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
    ):
        report = wifi_setup.scan_networks_report()

    assert report["networks"] == []
    assert report["scan"]["degraded"] is True
    assert report["scan"]["reason"] == "driver_scan_suppressed"
    assert report["scan"]["debug"]["listReturncode"] == 1


def test_scan_report_marks_driver_suppression_from_kernel_log(monkeypatch):
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
        wifi_setup, "_recent_kernel_scan_suppressed", return_value=True,
    ):
        report = wifi_setup.scan_networks_report()

    assert report["scan"]["degraded"] is True
    assert report["scan"]["reason"] == "driver_scan_suppressed"
    assert report["scan"]["debug"]["onlyCurrentNetwork"] is True


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
