# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jasper.bluetooth.engine import BluetoothEngine
from jasper.bluetooth.models import BluetoothDevice, UUID_HOGP


def _wiim_device() -> BluetoothDevice:
    return BluetoothDevice.from_props(
        "/org/bluez/hci0/dev_CA_AC_04_04_09_D7",
        {
            "Address": "CA:AC:04:04:09:D7",
            "Name": "WiiM Remote 2",
            "Alias": "WiiM Remote 2",
            "UUIDs": [f"{UUID_HOGP}0000-1000-8000-00805f9b34fb"],
            "Paired": True,
            "Connected": True,
            "Trusted": True,
            "ServicesResolved": True,
        },
    )


class _FakeObserver:
    def __init__(self, device: BluetoothDevice) -> None:
        self._device = device

    def get_by_mac(self, _mac: str) -> BluetoothDevice:
        return self._device


class _FakeDeviceIface:
    def __init__(self) -> None:
        self.connected = False
        self.paired = False

    async def call_connect(self) -> None:
        self.connected = True

    async def call_pair(self) -> None:
        self.paired = True


class _FakePropsIface:
    def __init__(self, device: BluetoothDevice) -> None:
        self._device = device
        self.trusted = False

    async def call_set(self, _iface: str, name: str, value) -> None:
        if name == "Trusted":
            self.trusted = bool(value.value)

    async def call_get_all(self, _iface: str) -> dict:
        return {
            "Address": self._device.address,
            "Name": self._device.name,
            "Alias": self._device.name,
            "UUIDs": self._device.uuids,
            "Paired": True,
            "Connected": True,
            "Trusted": True,
            "ServicesResolved": True,
        }


class _FakeProxy:
    def __init__(self, device: BluetoothDevice) -> None:
        self.device = _FakeDeviceIface()
        self.props = _FakePropsIface(device)

    def get_interface(self, name: str):
        if name == "org.bluez.Device1":
            return self.device
        if name == "org.freedesktop.DBus.Properties":
            return self.props
        raise AssertionError(f"unexpected interface {name}")


class _FakeBus:
    def __init__(self, device: BluetoothDevice) -> None:
        self.proxy = _FakeProxy(device)

    async def introspect(self, _bus: str, _path: str):
        return object()

    def get_proxy_object(self, _bus: str, _path: str, _intro):
        return self.proxy


def _engine(reasons: list[str]) -> BluetoothEngine:
    device = _wiim_device()

    async def reconcile(reason: str) -> object:
        reasons.append(reason)
        return object()

    engine = BluetoothEngine(accessory_reconcile=reconcile)
    setattr(engine, "_bus", _FakeBus(device))
    setattr(engine, "_observer", _FakeObserver(device))
    setattr(
        engine,
        "_roles",
        SimpleNamespace(
            set=lambda *_args: None,
            remove=lambda *_args: None,
        ),
    )
    return engine


@pytest.mark.asyncio
async def test_pair_refreshes_accessory_profiles_before_ready_event():
    reasons: list[str] = []
    engine = _engine(reasons)

    events = [
        event async for event in engine.pair("CA:AC:04:04:09:D7", timeout_s=1.0)
    ]

    assert reasons == ["bluetooth-pair"]
    ready_index = next(i for i, event in enumerate(events) if event["stage"] == "ready")
    refresh_index = next(
        i for i, event in enumerate(events)
        if event.get("detail") == "Refreshing optional accessory profiles."
    )
    assert refresh_index < ready_index


@pytest.mark.asyncio
async def test_connect_refreshes_accessory_profiles_after_bluez_connect():
    reasons: list[str] = []
    engine = _engine(reasons)

    ok, msg = await engine.connect("CA:AC:04:04:09:D7")

    assert (ok, msg) == (True, "connected")
    assert reasons == ["bluetooth-connect"]


@pytest.mark.asyncio
async def test_forget_refreshes_accessory_profiles_after_pair_record_removed(
    monkeypatch,
):
    reasons: list[str] = []
    engine = _engine(reasons)

    async def remove_device(_mac: str, _adapter: str):
        return True, "forgotten"

    monkeypatch.setattr("jasper.bluetooth.adapter.remove_device", remove_device)

    ok, msg = await engine.forget("CA:AC:04:04:09:D7")

    assert (ok, msg) == (True, "forgotten")
    assert reasons == ["bluetooth-forget"]
