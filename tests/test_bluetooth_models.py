# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from jasper.bluetooth.models import (
    BluetoothDevice,
    UUID_BATTERY,
    UUID_HID,
    UUID_HOGP,
    is_hid_uuids,
)


def test_bluetooth_device_reports_battery_capability_without_reading() -> None:
    device = BluetoothDevice.from_props(
        "/org/bluez/hci0/dev_11_22_33_44_55_66",
        {
            "Address": "11:22:33:44:55:66",
            "Name": "ANTICATER_MINI",
            "UUIDs": [
                f"{UUID_HID}0000-1000-8000-00805f9b34fb",
                f"{UUID_BATTERY}0000-1000-8000-00805f9b34fb",
            ],
            "Connected": True,
            "ServicesResolved": False,
            "Paired": True,
        },
    )

    payload = device.to_json()
    assert payload["connected"] is True
    assert payload["servicesResolved"] is False
    assert payload["battery"] is None
    assert payload["batteryCapable"] is True


def test_is_hid_uuids_matches_classic_hid_and_hogp() -> None:
    # BR/EDR HID (0x1124) — keyboards, mice that pair as classic.
    assert is_hid_uuids([f"{UUID_HID}0000-1000-8000-00805f9b34fb"]) is True
    # HID over GATT (0x1812) — BLE HID, what the VK-01 / ANTICATER_MINI
    # actually advertises. The earlier UUID_HID-only check missed these,
    # which is why the BT-off warning has to cover both.
    assert is_hid_uuids([f"{UUID_HOGP}0000-1000-8000-00805f9b34fb"]) is True
    # Mixed UUID lists still match.
    assert is_hid_uuids([
        "00001800-0000-1000-8000-00805f9b34fb",  # GAP
        f"{UUID_HOGP}0000-1000-8000-00805f9b34fb",
        f"{UUID_BATTERY}0000-1000-8000-00805f9b34fb",
    ]) is True
    # Non-HID device (an A2DP speaker) — must not trigger the warning.
    assert is_hid_uuids([
        "0000110b-0000-1000-8000-00805f9b34fb",
        f"{UUID_BATTERY}0000-1000-8000-00805f9b34fb",
    ]) is False
    assert is_hid_uuids([]) is False


def test_bluetooth_device_uses_input_icon_for_hogp_without_bluez_hint() -> None:
    device = BluetoothDevice.from_props(
        "/org/bluez/hci0/dev_CA_AC_04_04_09_D7",
        {
            "Address": "CA:AC:04:04:09:D7",
            "Name": "WiiM Remote 2",
            "UUIDs": [f"{UUID_HOGP}0000-1000-8000-00805f9b34fb"],
            "Paired": True,
        },
    )

    assert device.icon == "input-keyboard"


def test_bluetooth_device_merges_battery_percentage_when_bluez_exposes_it() -> None:
    device = BluetoothDevice.from_props(
        "/org/bluez/hci0/dev_11_22_33_44_55_66",
        {
            "Address": "11:22:33:44:55:66",
            "Name": "ANTICATER_MINI",
            "UUIDs": [f"{UUID_BATTERY}0000-1000-8000-00805f9b34fb"],
            "Connected": True,
            "ServicesResolved": True,
            "Paired": True,
        },
        {"Percentage": 40},
    )

    payload = device.to_json()
    assert payload["battery"] == 40
    assert payload["batteryCapable"] is True
    assert payload["servicesResolved"] is True
