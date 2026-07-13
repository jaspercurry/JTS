# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from jasper.bluetooth.handlers import REGISTRY, pick
from jasper.bluetooth.handlers.a2dp_sink import A2DPSinkHandler
from jasper.bluetooth.handlers.default import DefaultHandler
from jasper.bluetooth.handlers.hid import HIDHandler
from jasper.bluetooth.models import (
    BluetoothDevice,
    UUID_A2DP_SINK,
    UUID_A2DP_SOURCE,
    UUID_HFP_HF,
    UUID_HID,
    UUID_HOGP,
)


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


def test_a2dp_handler_matches_each_supported_audio_profile() -> None:
    handler = A2DPSinkHandler()

    for profile in (UUID_A2DP_SINK, UUID_A2DP_SOURCE, UUID_HFP_HF):
        assert handler.applies_to(
            _device_with_uuids([f"{profile}0000-1000-8000-00805f9b34fb"]),
        )
    assert not handler.applies_to(_device_with_uuids([]))


def test_handler_registry_selects_first_match_and_keeps_default_last() -> None:
    assert isinstance(REGISTRY[-1], DefaultHandler)

    hid_and_audio = _device_with_uuids([
        f"{UUID_HID}0000-1000-8000-00805f9b34fb",
        f"{UUID_A2DP_SINK}0000-1000-8000-00805f9b34fb",
    ])
    assert isinstance(pick(hid_and_audio), HIDHandler)

    audio = _device_with_uuids([
        f"{UUID_A2DP_SOURCE}0000-1000-8000-00805f9b34fb",
    ])
    assert isinstance(pick(audio), A2DPSinkHandler)
    assert isinstance(pick(_device_with_uuids([])), DefaultHandler)
