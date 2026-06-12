"""Shared tests for ESP32 accessory onboarding."""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from jasper.cli import _esp32_onboard as onboard
from jasper.cli.dial_onboard import DIAL_PROFILE
from jasper.cli.satellite_onboard import SATELLITE_PROFILE


def test_nmcli_terse_round_trips_colon_ssid_and_psk(monkeypatch):
    commands: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        commands.append(cmd)
        assert kwargs["timeout"] == onboard.NMCLI_TIMEOUT_S
        if "--active" in cmd:
            return SimpleNamespace(stdout="wifi:Kitchen\\: IoT\n")
        assert cmd[-1] == "Kitchen: IoT"
        return SimpleNamespace(
            stdout=(
                "802-11-wireless.ssid:Kitchen\\: IoT\n"
                "802-11-wireless-security.psk:p\\:a\\\\ss\n"
            )
        )

    monkeypatch.setattr(onboard.shutil, "which", lambda name: "/usr/bin/nmcli")
    monkeypatch.setattr(onboard.subprocess, "run", fake_run)

    assert onboard._read_wifi_nm() == ("Kitchen: IoT", "p:a\\ss")
    assert len(commands) == 2


def test_nmcli_timeout_is_actionable(monkeypatch):
    def fake_sleeping_nmcli(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(onboard.shutil, "which", lambda name: "/usr/bin/nmcli")
    monkeypatch.setattr(onboard.subprocess, "run", fake_sleeping_nmcli)

    with pytest.raises(RuntimeError, match="NetworkManager or D-Bus may be wedged"):
        onboard._read_wifi_nm()


def test_flash_firmware_passes_esptool_timeout(tmp_path: Path, monkeypatch):
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"fake")
    seen_timeout = None

    def fake_sleeping_esptool(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        nonlocal seen_timeout
        seen_timeout = kwargs["timeout"]
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(onboard.subprocess, "run", fake_sleeping_esptool)

    with pytest.raises(subprocess.TimeoutExpired):
        onboard.flash_firmware(DIAL_PROFILE, "/dev/ttyACM0", firmware)
    assert seen_timeout == onboard.ESPTOOL_TIMEOUT_S


def test_run_onboard_maps_esptool_timeout_to_flash_exit(tmp_path: Path, monkeypatch):
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"fake")
    monkeypatch.setattr(onboard, "wait_for_online", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        onboard,
        "find_device",
        lambda *args, **kwargs: onboard.SerialDevice("/dev/ttyACM0", 0x303A, 0x1001),
    )
    monkeypatch.setattr(onboard, "probe_firmware", lambda *args, **kwargs: False)

    def fake_timeout(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(["esptool"], onboard.ESPTOOL_TIMEOUT_S)

    monkeypatch.setattr(onboard, "flash_firmware", fake_timeout)

    result = onboard.run_onboard(
        DIAL_PROFILE,
        ["--bin", str(firmware), "--ssid", "Home", "--password", "secret"],
    )

    assert result == 2


def test_auto_refuses_to_push_creds_without_positive_probe(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(onboard, "wait_for_online", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        onboard,
        "find_device",
        lambda *args, **kwargs: onboard.SerialDevice("/dev/ttyACM0", 0x303A, 0x1001),
    )
    monkeypatch.setattr(onboard, "probe_firmware", lambda *args, **kwargs: False)
    monkeypatch.setattr(onboard, "read_pi_wifi", lambda: calls.append("read_wifi"))
    monkeypatch.setattr(onboard, "push_credentials", lambda *args, **kwargs: calls.append("push"))

    result = onboard.run_onboard(DIAL_PROFILE, ["--auto"])

    assert result == 4
    assert calls == []


def test_satellite_profile_copy_matches_touchscreen_device():
    assert "knob" not in SATELLITE_PROFILE.done_message
    assert "satellite" in SATELLITE_PROFILE.done_message
    assert (
        SATELLITE_PROFILE.boot_log_description
        == "[boot] jasper-satellite-amoled firmware v<version>"
    )
    assert SATELLITE_PROFILE.boot_signature == b"jasper-satellite-amoled firmware"
