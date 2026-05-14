from __future__ import annotations

from jasper.bluetooth.models import BluetoothDevice, UUID_BATTERY, UUID_HID


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
