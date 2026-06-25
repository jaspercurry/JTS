# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from jasper.bluetooth.models import UUID_BATTERY_LEVEL
from jasper.bluetooth.scan import (
    _battery_level_characteristic_path,
    _battery_percent_from_read_value,
)


DEVICE_PATH = "/org/bluez/hci0/dev_CA_AC_04_04_09_D7"
BATTERY_CHAR_PATH = f"{DEVICE_PATH}/service001c/char001d"


def test_battery_level_characteristic_path_finds_standard_gatt_char() -> None:
    managed = {
        f"{DEVICE_PATH}/service001c": {
            "org.bluez.GattService1": {
                "UUID": "0000180f-0000-1000-8000-00805f9b34fb",
            },
        },
        BATTERY_CHAR_PATH: {
            "org.bluez.GattCharacteristic1": {
                "UUID": f"{UUID_BATTERY_LEVEL}0000-1000-8000-00805f9b34fb",
                "Flags": ["read", "notify"],
            },
        },
        "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF/service001c/char001d": {
            "org.bluez.GattCharacteristic1": {
                "UUID": f"{UUID_BATTERY_LEVEL}0000-1000-8000-00805f9b34fb",
            },
        },
    }

    assert _battery_level_characteristic_path(managed, DEVICE_PATH) == (
        BATTERY_CHAR_PATH
    )


def test_battery_level_characteristic_path_returns_none_when_absent() -> None:
    managed = {
        f"{DEVICE_PATH}/service0020/char0021": {
            "org.bluez.GattCharacteristic1": {
                "UUID": "00002a4a-0000-1000-8000-00805f9b34fb",
            },
        },
    }

    assert _battery_level_characteristic_path(managed, DEVICE_PATH) is None


def test_battery_percent_from_read_value_decodes_single_byte() -> None:
    assert _battery_percent_from_read_value([60]) == 60
    assert _battery_percent_from_read_value(bytes([100])) == 100
    assert _battery_percent_from_read_value(bytearray([0])) == 0


def test_battery_percent_from_read_value_rejects_invalid_values() -> None:
    assert _battery_percent_from_read_value([]) is None
    assert _battery_percent_from_read_value([101]) is None
    assert _battery_percent_from_read_value(["not-a-byte"]) is None
