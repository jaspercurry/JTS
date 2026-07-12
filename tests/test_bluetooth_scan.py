# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from jasper.bluetooth import scan
from jasper.bluetooth.models import (
    UUID_BATTERY,
    UUID_BATTERY_LEVEL,
    BluetoothDevice,
)
from jasper.bluetooth.scan import (
    DeviceObserver,
    _battery_level_characteristic_path,
    _battery_percent_from_read_value,
)


DEVICE_PATH = "/org/bluez/hci0/dev_CA_AC_04_04_09_D7"
BATTERY_CHAR_PATH = f"{DEVICE_PATH}/service001c/char001d"
DEVICE_IFACE = "org.bluez.Device1"
BATTERY_IFACE = "org.bluez.Battery1"
PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"
OBJECT_MANAGER_IFACE = "org.freedesktop.DBus.ObjectManager"
GATT_IFACE = "org.bluez.GattCharacteristic1"


def _device_props(
    address: str,
    *,
    connected: bool = False,
    services_resolved: bool = False,
    rssi: int = -60,
) -> dict[str, object]:
    return {
        "Address": address,
        "Name": f"Test device {address[-2:]}",
        "RSSI": rssi,
        "UUIDs": [f"{UUID_BATTERY}0000-1000-8000-00805f9b34fb"],
        "Connected": connected,
        "ServicesResolved": services_resolved,
    }


class _FakeProperties:
    def __init__(self) -> None:
        self.callbacks: list = []
        self.off_calls = 0

    def on_properties_changed(self, callback) -> None:
        self.callbacks.append(callback)

    def off_properties_changed(self, callback) -> None:
        self.off_calls += 1
        if callback in self.callbacks:
            self.callbacks.remove(callback)

    def emit(self, iface: str, changed: dict, invalidated: list[str]) -> None:
        for callback in list(self.callbacks):
            callback(iface, changed, invalidated)


class _FakeCharacteristic:
    def __init__(
        self,
        value: int,
        *,
        release: asyncio.Event | None = None,
        ignore_cancellation: bool = False,
    ) -> None:
        self.value = value
        self.release = release
        self.ignore_cancellation = ignore_cancellation
        self.started = asyncio.Event()
        self.read_calls = 0
        self.cancelled = False

    async def call_read_value(self, _options: dict) -> list[int]:
        self.read_calls += 1
        self.started.set()
        if self.release is not None:
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                if not self.ignore_cancellation:
                    raise
                await self.release.wait()
        return [self.value]


class _FakeObjectManager:
    def __init__(self, managed: dict[str, dict]) -> None:
        self.managed = managed
        self.added_callbacks: list = []
        self.removed_callbacks: list = []

    async def call_get_managed_objects(self) -> dict[str, dict]:
        return self.managed

    def on_interfaces_added(self, callback) -> None:
        self.added_callbacks.append(callback)

    def off_interfaces_added(self, callback) -> None:
        if callback in self.added_callbacks:
            self.added_callbacks.remove(callback)

    def on_interfaces_removed(self, callback) -> None:
        self.removed_callbacks.append(callback)

    def off_interfaces_removed(self, callback) -> None:
        if callback in self.removed_callbacks:
            self.removed_callbacks.remove(callback)

    def emit_added(self, path: str, ifaces: dict) -> None:
        self.managed.setdefault(path, {}).update(ifaces)
        for callback in list(self.added_callbacks):
            callback(path, ifaces)

    def emit_removed(self, path: str, interfaces: list[str]) -> None:
        existing = self.managed.get(path, {})
        for iface in interfaces:
            existing.pop(iface, None)
        if not existing:
            self.managed.pop(path, None)
        for callback in list(self.removed_callbacks):
            callback(path, interfaces)


class _FakeProxy:
    def __init__(self, bus: "_FakeBus", path: str) -> None:
        self.bus = bus
        self.path = path

    def get_interface(self, iface: str):
        if self.path == "/" and iface == OBJECT_MANAGER_IFACE:
            return self.bus.om
        if iface == PROPERTIES_IFACE:
            return self.bus.properties.setdefault(self.path, _FakeProperties())
        if iface == GATT_IFACE:
            return self.bus.characteristics[self.path]
        raise KeyError((self.path, iface))


class _FakeBus:
    def __init__(
        self,
        managed: dict[str, dict],
        *,
        characteristics: dict[str, _FakeCharacteristic] | None = None,
    ) -> None:
        self.om = _FakeObjectManager(managed)
        self.properties: dict[str, _FakeProperties] = {}
        self.characteristics = characteristics or {}
        self.disconnected = False

    async def connect(self) -> "_FakeBus":
        return self

    async def introspect(self, _bus_name: str, path: str) -> str:
        await asyncio.sleep(0)
        return path

    def get_proxy_object(
        self,
        _bus_name: str,
        path: str,
        _intro: str,
    ) -> _FakeProxy:
        return _FakeProxy(self, path)

    def disconnect(self) -> None:
        self.disconnected = True


async def _settle() -> None:
    for _ in range(6):
        await asyncio.sleep(0)


async def _next_event(
    events: AsyncIterator[tuple[str, BluetoothDevice]],
) -> tuple[str, BluetoothDevice]:
    return await asyncio.wait_for(anext(events), timeout=1.0)


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


@pytest.mark.asyncio
async def test_device_observer_tracks_interfaces_without_ghost_resurrection(
    monkeypatch,
) -> None:
    second_path = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
    managed = {
        DEVICE_PATH: {
            DEVICE_IFACE: _device_props("CA:AC:04:04:09:D7"),
            BATTERY_IFACE: {"Percentage": 40},
        }
    }
    bus = _FakeBus(managed)
    monkeypatch.setattr(scan, "MessageBus", lambda **_kwargs: bus)
    observer = DeviceObserver()

    await observer.start()
    subscription = await observer.subscribe()
    events = subscription.events()
    action, device = await _next_event(events)
    assert (action, device.path, device.battery) == ("add", DEVICE_PATH, 40)
    await _settle()

    first_props = bus.properties[DEVICE_PATH]
    assert len(first_props.callbacks) == 2
    first_props.emit(DEVICE_IFACE, {"RSSI": -35}, [])
    action, device = await _next_event(events)
    assert (action, device.rssi) == ("update", -35)
    first_props.emit(DEVICE_IFACE, {}, ["RSSI"])
    action, device = await _next_event(events)
    assert (action, device.rssi) == ("update", None)

    bus.om.emit_added(
        second_path,
        {DEVICE_IFACE: _device_props("AA:BB:CC:DD:EE:FF", rssi=-70)},
    )
    action, device = await _next_event(events)
    assert (action, device.path, device.battery) == ("add", second_path, None)
    await _settle()

    bus.om.emit_added(second_path, {BATTERY_IFACE: {"Percentage": 60}})
    action, device = await _next_event(events)
    assert (action, device.battery) == ("update", 60)
    await _settle()
    second_props = bus.properties[second_path]
    stale_battery_callbacks = list(second_props.callbacks)
    second_props.emit(BATTERY_IFACE, {"Percentage": 61}, [])
    action, device = await _next_event(events)
    assert (action, device.battery) == ("update", 61)
    second_props.emit(BATTERY_IFACE, {}, ["Percentage"])
    action, device = await _next_event(events)
    assert (action, device.battery) == ("update", None)

    # BlueZ can re-announce Device1 at an existing path. Without an attached
    # Battery1 interface this is a fresh device instance and must not inherit
    # the old instance's battery cache or callback.
    bus.om.emit_added(
        second_path,
        {DEVICE_IFACE: _device_props("AA:BB:CC:DD:EE:FF", rssi=-50)},
    )
    action, device = await _next_event(events)
    assert (action, device.battery, device.rssi) == ("add", None, -50)
    await _settle()
    for callback in stale_battery_callbacks:
        callback(BATTERY_IFACE, {"Percentage": 99}, [])
    observed = observer.get(second_path)
    assert observed is not None
    assert observed.battery is None
    assert subscription._q.empty()

    bus.om.emit_added(second_path, {BATTERY_IFACE: {"Percentage": 70}})
    action, device = await _next_event(events)
    assert (action, device.battery) == ("update", 70)
    await _settle()
    live_callbacks = list(second_props.callbacks)
    bus.om.emit_removed(second_path, [BATTERY_IFACE])
    action, device = await _next_event(events)
    assert (action, device.battery) == ("update", None)
    for callback in live_callbacks:
        callback(BATTERY_IFACE, {"Percentage": 98}, [])
    observed = observer.get(second_path)
    assert observed is not None
    assert observed.battery is None
    assert subscription._q.empty()

    queued_device_callbacks = list(second_props.callbacks)
    bus.om.emit_removed(second_path, [DEVICE_IFACE])
    action, device = await _next_event(events)
    assert (action, device.path) == ("remove", second_path)
    for callback in queued_device_callbacks:
        callback(DEVICE_IFACE, {"Name": "ghost"}, [])
    assert observer.get(second_path) is None
    assert subscription._q.empty()

    subscription.close()
    await observer.stop()

    assert bus.om.added_callbacks == []
    assert bus.om.removed_callbacks == []
    assert all(props.callbacks == [] for props in bus.properties.values())
    assert observer._device_unsubscribes == {}
    assert observer._battery_unsubscribes == {}
    assert observer._battery_read_tasks == {}
    assert observer._watch_tasks == set()
    assert bus.disconnected is True


@pytest.mark.asyncio
async def test_device_observer_direct_battery_read_updates_once_and_stays_removed(
    monkeypatch,
) -> None:
    stale_path = "/org/bluez/hci0/dev_11_22_33_44_55_66"
    stale_char_path = f"{stale_path}/service001c/char001d"
    battery_release = asyncio.Event()
    device_release = asyncio.Event()
    direct = _FakeCharacteristic(72)
    stale_battery = _FakeCharacteristic(
        88,
        release=battery_release,
        ignore_cancellation=True,
    )
    stale_device = _FakeCharacteristic(
        89,
        release=device_release,
        ignore_cancellation=True,
    )
    managed: dict[str, dict] = {
        DEVICE_PATH: {
            DEVICE_IFACE: _device_props(
                "CA:AC:04:04:09:D7",
                connected=True,
                services_resolved=True,
            )
        },
        BATTERY_CHAR_PATH: {
            GATT_IFACE: {"UUID": f"{UUID_BATTERY_LEVEL}0000-1000-8000-00805f9b34fb"}
        },
    }
    bus = _FakeBus(
        managed,
        characteristics={
            BATTERY_CHAR_PATH: direct,
            stale_char_path: stale_battery,
        },
    )
    monkeypatch.setattr(scan, "MessageBus", lambda **_kwargs: bus)
    observer = DeviceObserver()

    await observer.start()
    subscription = await observer.subscribe()
    events = subscription.events()
    action, device = await _next_event(events)
    assert (action, device.battery) == ("add", None)
    action, device = await _next_event(events)
    assert (action, device.battery) == ("update", 72)
    await _settle()
    assert direct.read_calls == 1
    assert subscription._q.empty()

    bus.om.emit_added(
        stale_path,
        {
            DEVICE_IFACE: _device_props(
                "11:22:33:44:55:66",
                connected=True,
                services_resolved=True,
            ),
            BATTERY_IFACE: {"Percentage": 44},
        },
    )
    bus.om.managed[stale_char_path] = {
        GATT_IFACE: {"UUID": f"{UUID_BATTERY_LEVEL}0000-1000-8000-00805f9b34fb"}
    }
    action, device = await _next_event(events)
    assert (action, device.path, device.battery) == ("add", stale_path, 44)
    await asyncio.wait_for(stale_battery.started.wait(), timeout=1.0)
    battery_task = observer._battery_read_tasks[stale_path]

    bus.om.emit_removed(stale_path, [BATTERY_IFACE])
    action, device = await _next_event(events)
    assert (action, device.path, device.battery) == ("update", stale_path, None)
    await _settle()
    assert stale_battery.cancelled is True
    battery_release.set()
    await asyncio.wait_for(battery_task, timeout=1.0)
    await _settle()
    observed = observer.get(stale_path)
    assert observed is not None
    assert observed.battery is None
    assert stale_path not in observer._battery_props
    assert stale_path not in observer._battery_read_tasks
    assert subscription._q.empty()

    bus.characteristics[stale_char_path] = stale_device
    bus.om.emit_added(stale_path, {BATTERY_IFACE: {"Percentage": 45}})
    action, device = await _next_event(events)
    assert (action, device.path, device.battery) == ("update", stale_path, 45)
    await asyncio.wait_for(stale_device.started.wait(), timeout=1.0)
    device_task = observer._battery_read_tasks[stale_path]

    bus.om.emit_removed(stale_path, [DEVICE_IFACE])
    action, device = await _next_event(events)
    assert (action, device.path) == ("remove", stale_path)
    await _settle()
    assert stale_device.cancelled is True
    device_release.set()
    await asyncio.wait_for(device_task, timeout=1.0)
    await _settle()

    assert observer.get(stale_path) is None
    assert stale_path not in observer._battery_props
    assert stale_path not in observer._battery_read_tasks
    assert subscription._q.empty()

    stop_path = "/org/bluez/hci0/dev_22_33_44_55_66_77"
    stop_char_path = f"{stop_path}/service001c/char001d"
    stop_blocked = _FakeCharacteristic(90, release=asyncio.Event())
    bus.characteristics[stop_char_path] = stop_blocked
    bus.om.emit_added(
        stop_path,
        {
            DEVICE_IFACE: _device_props(
                "22:33:44:55:66:77",
                connected=True,
                services_resolved=True,
            )
        },
    )
    bus.om.managed[stop_char_path] = {
        GATT_IFACE: {"UUID": f"{UUID_BATTERY_LEVEL}0000-1000-8000-00805f9b34fb"}
    }
    action, device = await _next_event(events)
    assert (action, device.path) == ("add", stop_path)
    await asyncio.wait_for(stop_blocked.started.wait(), timeout=1.0)

    subscription.close()
    await observer.stop()

    assert stop_blocked.cancelled is True
    assert bus.om.added_callbacks == []
    assert bus.om.removed_callbacks == []
    assert all(props.callbacks == [] for props in bus.properties.values())
    assert observer._device_unsubscribes == {}
    assert observer._battery_unsubscribes == {}
    assert observer._battery_read_tasks == {}
    assert observer._watch_tasks == set()
    assert bus.disconnected is True
