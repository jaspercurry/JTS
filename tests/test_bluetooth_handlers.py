# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from jasper.bluetooth.handlers.hid import HIDHandler
from jasper.bluetooth.models import BluetoothDevice, UUID_HID, UUID_HOGP


def _device_with_uuids(uuids: list[str]) -> BluetoothDevice:
    return BluetoothDevice.from_props(
        "/org/bluez/hci0/dev_CA_AC_04_04_09_D7",
        {
            "Address": "CA:AC:04:04:09:D7",
            "Name": "WiiM Remote 2",
            "UUIDs": uuids,
            "Connected": True,
            "ServicesResolved": True,
            "Paired": True,
        },
    )


def test_hid_handler_matches_classic_hid_and_ble_hogp() -> None:
    handler = HIDHandler()

    assert handler.applies_to(
        _device_with_uuids([f"{UUID_HID}0000-1000-8000-00805f9b34fb"]),
    )
    assert handler.applies_to(
        _device_with_uuids([f"{UUID_HOGP}0000-1000-8000-00805f9b34fb"]),
    )


def test_hid_handler_ignores_non_hid_devices() -> None:
    handler = HIDHandler()

    assert not handler.applies_to(
        _device_with_uuids(["0000110b-0000-1000-8000-00805f9b34fb"]),
    )
