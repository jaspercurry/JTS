# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
from dbus_next.errors import DBusError

from jasper.bluetooth import engine as engine_module
from jasper.bluetooth.engine import BluetoothEngine, _format_dbus_error
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


class _FakeAdapter:
    def __init__(
        self,
        *,
        start_error: DBusError | None = None,
        block_start: bool = False,
        accept_start_before_reply: bool = False,
        stop_error: DBusError | None = None,
        block_stop: bool = False,
    ) -> None:
        self.discovering = False
        self.start_calls = 0
        self.stop_calls = 0
        self.start_error = start_error
        self.accept_start_before_reply = accept_start_before_reply
        self.stop_error = stop_error
        self.start_entered = asyncio.Event()
        self.release_start = asyncio.Event()
        self.stop_entered = asyncio.Event()
        self.release_stop = asyncio.Event()
        if not block_start:
            self.release_start.set()
        if not block_stop:
            self.release_stop.set()

    async def call_start_discovery(self) -> None:
        self.start_calls += 1
        self.start_entered.set()
        if self.accept_start_before_reply:
            self.discovering = True
        await self.release_start.wait()
        if self.start_error is not None:
            raise self.start_error
        if not self.accept_start_before_reply:
            self.discovering = True

    async def call_stop_discovery(self) -> None:
        self.stop_calls += 1
        self.stop_entered.set()
        await self.release_stop.wait()
        if self.stop_error is not None:
            raise self.stop_error
        self.discovering = False


class _FakeScanProxy:
    def __init__(self, adapter: _FakeAdapter) -> None:
        self.adapter = adapter

    def get_interface(self, name: str) -> _FakeAdapter:
        assert name == "org.bluez.Adapter1"
        return self.adapter


class _FakeScanBus:
    def __init__(
        self,
        adapter: _FakeAdapter,
        *,
        block_introspect: bool = False,
        device: BluetoothDevice | None = None,
    ) -> None:
        self.adapter = adapter
        self.proxy = _FakeScanProxy(adapter)
        self.device_path = device.path if device is not None else None
        self.device_proxy = _FakeProxy(device) if device is not None else None
        self.disconnected = False
        self.introspect_entered = asyncio.Event()
        self.release_introspect = asyncio.Event()
        if not block_introspect:
            self.release_introspect.set()

    async def introspect(self, bus: str, path: str) -> object:
        assert bus == "org.bluez"
        assert path in {"/org/bluez/hci0", self.device_path}
        self.introspect_entered.set()
        await self.release_introspect.wait()
        return object()

    async def connect(self) -> _FakeScanBus:
        return self

    def get_proxy_object(
        self,
        bus: str,
        path: str,
        _intro: object,
    ) -> _FakeScanProxy | _FakeProxy:
        assert bus == "org.bluez"
        if path != "/org/bluez/hci0":
            assert path == self.device_path
            assert self.device_proxy is not None
            return self.device_proxy
        return self.proxy

    def disconnect(self) -> None:
        self.disconnected = True
        # BlueZ ends discovery when its initiating bus client disconnects.
        self.adapter.discovering = False


class _FakeScanObserver:
    def __init__(self) -> None:
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


def _scan_engine(adapter: _FakeAdapter) -> BluetoothEngine:
    engine = BluetoothEngine()
    setattr(engine, "_bus", _FakeScanBus(adapter))
    setattr(engine, "_observer", _FakeScanObserver())
    return engine


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


def _shared_bus_engine(
    adapter: _FakeAdapter,
) -> tuple[BluetoothEngine, _FakeScanBus, list[str]]:
    device = _wiim_device()
    reasons: list[str] = []

    async def reconcile(reason: str) -> object:
        reasons.append(reason)
        return object()

    engine = BluetoothEngine(accessory_reconcile=reconcile)
    bus = _FakeScanBus(adapter, device=device)
    setattr(engine, "_bus", bus)
    setattr(engine, "_observer", _FakeObserver(device))
    setattr(
        engine,
        "_roles",
        SimpleNamespace(
            set=lambda *_args: None,
            remove=lambda *_args: None,
        ),
    )
    return engine, bus, reasons


@pytest.mark.asyncio
async def test_pair_refreshes_accessory_profiles_before_ready_event():
    reasons: list[str] = []
    engine = _engine(reasons)

    events = [event async for event in engine.pair("CA:AC:04:04:09:D7", timeout_s=1.0)]

    assert reasons == ["bluetooth-pair"]
    ready_index = next(i for i, event in enumerate(events) if event["stage"] == "ready")
    refresh_index = next(
        i
        for i, event in enumerate(events)
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
    caplog,
):
    reasons: list[str] = []
    engine = _engine(reasons)

    async def remove_device(_mac: str, _adapter: str):
        return True, "forgotten"

    monkeypatch.setattr("jasper.bluetooth.adapter.remove_device", remove_device)

    caplog.set_level(logging.INFO, logger="jasper.bluetooth.engine")
    ok, msg = await engine.forget("CA:AC:04:04:09:D7")

    assert (ok, msg) == (True, "forgotten")
    assert reasons == ["bluetooth-forget"]
    assert "event=bluetooth.device_forget" in caplog.text
    assert "address=CA:AC:04:04:09:D7" in caplog.text
    assert "ok=true" in caplog.text


@pytest.mark.asyncio
async def test_scan_natural_expiry_stops_bluez_and_clears_task_identity():
    adapter = _FakeAdapter()
    engine = _scan_engine(adapter)

    await engine.start_discovery(duration_s=0)
    expiry = engine._scan_task
    assert expiry is not None
    await expiry

    assert adapter.start_calls == 1
    assert adapter.stop_calls == 1
    assert adapter.discovering is False
    assert engine._scan_task is None


@pytest.mark.asyncio
async def test_scan_start_before_engine_start_creates_no_timer():
    engine = BluetoothEngine()

    await engine.start_discovery(duration_s=0)

    assert engine._scan_task is None


@pytest.mark.asyncio
async def test_scan_manual_stop_cancels_expiry_and_stops_bluez():
    adapter = _FakeAdapter()
    engine = _scan_engine(adapter)

    await engine.start_discovery(duration_s=60)
    expiry = engine._scan_task
    assert expiry is not None
    await engine.stop_discovery()
    await asyncio.sleep(0)

    assert expiry.done()
    assert adapter.stop_calls == 1
    assert adapter.discovering is False
    assert engine._scan_task is None


@pytest.mark.asyncio
async def test_scan_refresh_replaces_timer_without_stacking_stop_calls():
    adapter = _FakeAdapter(block_stop=True)
    engine = _scan_engine(adapter)

    await engine.start_discovery(duration_s=60)
    first_expiry = engine._scan_task
    await engine.start_discovery(duration_s=0)
    refreshed_expiry = engine._scan_task
    assert first_expiry is not None
    assert refreshed_expiry is not None
    assert refreshed_expiry is not first_expiry
    await adapter.stop_entered.wait()
    adapter.release_stop.set()
    await refreshed_expiry
    await asyncio.sleep(0)

    assert first_expiry.done()
    assert adapter.start_calls == 2
    assert adapter.stop_calls == 1
    assert adapter.discovering is False
    assert engine._scan_task is None


@pytest.mark.asyncio
async def test_failed_scan_refresh_preserves_prior_timer_and_deadline():
    adapter = _FakeAdapter()
    engine = _scan_engine(adapter)

    await engine.start_discovery(duration_s=0.05)
    first_expiry = engine._scan_task
    assert first_expiry is not None
    adapter.start_error = DBusError("org.bluez.Error.Failed", "refresh failed")

    with pytest.raises(DBusError, match="refresh failed"):
        await engine.start_discovery(duration_s=60)

    assert engine._scan_task is first_expiry
    assert not first_expiry.done()
    await first_expiry
    assert adapter.stop_calls == 1
    assert adapter.discovering is False
    assert engine._scan_task is None


@pytest.mark.asyncio
async def test_scan_refresh_wins_exact_expiry_race_and_remains_discovering():
    adapter = _FakeAdapter(block_stop=True)
    engine = _scan_engine(adapter)

    await engine.start_discovery(duration_s=0)
    first_expiry = engine._scan_task
    assert first_expiry is not None
    await adapter.stop_entered.wait()

    refresh = asyncio.create_task(engine.start_discovery(duration_s=60))
    await asyncio.sleep(0)
    assert not refresh.done()
    adapter.release_stop.set()
    await refresh

    refreshed_expiry = engine._scan_task
    assert refreshed_expiry is not None
    assert refreshed_expiry is not first_expiry
    assert adapter.start_calls == 2
    assert adapter.stop_calls == 1
    assert adapter.discovering is True

    await engine.stop_discovery()


@pytest.mark.asyncio
async def test_scan_start_introspection_timeout_creates_no_timer(monkeypatch):
    adapter = _FakeAdapter()
    bus = _FakeScanBus(adapter, block_introspect=True)
    engine = _scan_engine(adapter)
    setattr(engine, "_bus", bus)
    monkeypatch.setattr(engine_module, "SCAN_DBUS_TIMEOUT_SEC", 0.01)

    with pytest.raises(asyncio.TimeoutError, match="adapter introspection timed out"):
        await engine.start_discovery(duration_s=60)

    assert engine._scan_task is None
    assert adapter.start_calls == 0
    assert adapter.discovering is False

    bus.release_introspect.set()
    await engine.start_discovery(duration_s=0)
    expiry = engine._scan_task
    assert expiry is not None
    await expiry
    assert adapter.discovering is False


@pytest.mark.asyncio
async def test_scan_start_bluez_timeout_releases_lock_for_retry(monkeypatch):
    adapter = _FakeAdapter(block_start=True)
    engine = _scan_engine(adapter)
    monkeypatch.setattr(engine_module, "SCAN_DBUS_TIMEOUT_SEC", 0.01)

    with pytest.raises(asyncio.TimeoutError, match="StartDiscovery timed out"):
        await engine.start_discovery(duration_s=60)

    assert engine._scan_task is None
    assert adapter.discovering is False
    adapter.release_start.set()
    await engine.start_discovery(duration_s=0)
    expiry = engine._scan_task
    assert expiry is not None
    await expiry
    assert adapter.discovering is False


@pytest.mark.asyncio
async def test_scan_start_timeout_stops_discovery_accepted_before_reply(monkeypatch):
    adapter = _FakeAdapter(
        block_start=True,
        accept_start_before_reply=True,
    )
    engine = _scan_engine(adapter)
    bus = engine._bus
    monkeypatch.setattr(engine_module, "SCAN_DBUS_TIMEOUT_SEC", 0.01)

    with pytest.raises(asyncio.TimeoutError, match="StartDiscovery timed out"):
        await engine.start_discovery(duration_s=60)

    assert adapter.start_calls == 1
    assert adapter.stop_calls == 1
    assert adapter.discovering is False
    assert engine._scan_task is None
    assert engine._bus is bus
    assert bus is not None and bus.disconnected is False


@pytest.mark.asyncio
async def test_scan_start_timeout_releases_bus_when_cleanup_times_out(
    monkeypatch,
    caplog,
):
    adapter = _FakeAdapter(
        block_start=True,
        accept_start_before_reply=True,
        block_stop=True,
    )
    engine = _scan_engine(adapter)
    bus = engine._bus
    monkeypatch.setattr(engine_module, "SCAN_DBUS_TIMEOUT_SEC", 0.01)
    caplog.set_level(logging.WARNING, logger="jasper.bluetooth.engine")

    with pytest.raises(asyncio.TimeoutError, match="StartDiscovery timed out"):
        await engine.start_discovery(duration_s=60)

    assert adapter.stop_calls == 1
    assert adapter.discovering is False
    assert engine._scan_task is None
    assert engine._bus is None
    assert engine._bus_recovery_required is True
    assert bus is not None and bus.disconnected is True
    assert "event=bluetooth.scan_start_cleanup_failed" in caplog.text
    assert "error_type=TimeoutError" in caplog.text
    assert "BlueZ StopDiscovery timed out after 0.01s" in caplog.text


@pytest.mark.asyncio
async def test_pair_recovers_shared_bus_after_auto_stop_failure(monkeypatch):
    first_adapter = _FakeAdapter(block_stop=True)
    engine, first_bus, reasons = _shared_bus_engine(first_adapter)
    replacement_bus = _FakeScanBus(_FakeAdapter(), device=_wiim_device())
    monkeypatch.setattr(engine_module, "SCAN_DBUS_TIMEOUT_SEC", 0.01)
    monkeypatch.setattr(
        engine_module,
        "MessageBus",
        lambda *, bus_type: replacement_bus,
    )

    await engine.start_discovery(duration_s=0)
    expiry = engine._scan_task
    assert expiry is not None
    await expiry
    assert first_bus.disconnected is True
    assert engine._bus is None

    events = [
        event
        async for event in engine.pair(
            "CA:AC:04:04:09:D7",
            timeout_s=1.0,
        )
    ]

    assert engine._bus is replacement_bus
    assert engine._bus_recovery_required is False
    assert any(event["stage"] == "ready" for event in events)
    assert replacement_bus.device_proxy is not None
    assert replacement_bus.device_proxy.device.paired is True
    assert reasons == ["bluetooth-pair"]


@pytest.mark.asyncio
async def test_connect_recovers_shared_bus_after_scan_start_cleanup_failure(
    monkeypatch,
):
    first_adapter = _FakeAdapter(
        block_start=True,
        accept_start_before_reply=True,
        block_stop=True,
    )
    engine, first_bus, reasons = _shared_bus_engine(first_adapter)
    replacement_bus = _FakeScanBus(_FakeAdapter(), device=_wiim_device())
    monkeypatch.setattr(engine_module, "SCAN_DBUS_TIMEOUT_SEC", 0.01)
    monkeypatch.setattr(
        engine_module,
        "MessageBus",
        lambda *, bus_type: replacement_bus,
    )

    with pytest.raises(asyncio.TimeoutError, match="StartDiscovery timed out"):
        await engine.start_discovery(duration_s=60)

    assert first_bus.disconnected is True
    assert engine._bus is None
    assert await engine.connect("CA:AC:04:04:09:D7") == (True, "connected")
    assert engine._bus is replacement_bus
    assert engine._bus_recovery_required is False
    assert replacement_bus.device_proxy is not None
    assert replacement_bus.device_proxy.device.connected is True
    assert reasons == ["bluetooth-connect"]


@pytest.mark.asyncio
async def test_concurrent_device_operations_share_one_recovered_bus(monkeypatch):
    replacement_bus = _FakeScanBus(_FakeAdapter(), device=_wiim_device())

    class _BlockingConnector:
        def __init__(self) -> None:
            self.connect_calls = 0
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def connect(self):
            self.connect_calls += 1
            self.entered.set()
            await self.release.wait()
            return replacement_bus

    engine, _first_bus, _reasons = _shared_bus_engine(_FakeAdapter())
    engine._release_scan_owner_bus(engine._bus, reason="test")
    connector = _BlockingConnector()
    monkeypatch.setattr(
        engine_module,
        "MessageBus",
        lambda *, bus_type: connector,
    )

    async def collect_pair_events() -> list[dict]:
        return [
            event
            async for event in engine.pair(
                "CA:AC:04:04:09:D7",
                timeout_s=1.0,
            )
        ]

    pair_task = asyncio.create_task(collect_pair_events())
    connect_task = asyncio.create_task(engine.connect("CA:AC:04:04:09:D7"))
    await connector.entered.wait()
    await asyncio.sleep(0)
    assert not pair_task.done()
    assert not connect_task.done()

    connector.release.set()
    pair_events, connect_result = await asyncio.gather(pair_task, connect_task)

    assert connector.connect_calls == 1
    assert engine._bus is replacement_bus
    assert engine._bus_recovery_required is False
    assert any(event["stage"] == "ready" for event in pair_events)
    assert connect_result == (True, "connected")


@pytest.mark.asyncio
async def test_scan_request_recovers_bus_after_fail_closed_release(monkeypatch):
    first_adapter = _FakeAdapter(
        block_start=True,
        accept_start_before_reply=True,
        block_stop=True,
    )
    engine = _scan_engine(first_adapter)
    first_bus = engine._bus
    replacement_adapter = _FakeAdapter()
    replacement_bus = _FakeScanBus(replacement_adapter)
    monkeypatch.setattr(engine_module, "SCAN_DBUS_TIMEOUT_SEC", 0.01)
    monkeypatch.setattr(
        engine_module,
        "MessageBus",
        lambda *, bus_type: replacement_bus,
    )

    with pytest.raises(asyncio.TimeoutError, match="StartDiscovery timed out"):
        await engine.start_discovery(duration_s=60)

    assert first_bus is not None and first_bus.disconnected is True
    assert engine._bus is None
    await engine.start_discovery(duration_s=0)
    expiry = engine._scan_task
    assert expiry is not None
    await expiry

    assert engine._bus is replacement_bus
    assert engine._bus_recovery_required is False
    assert replacement_adapter.start_calls == 1
    assert replacement_adapter.stop_calls == 1
    assert replacement_adapter.discovering is False


@pytest.mark.asyncio
async def test_scan_auto_stop_timeout_logs_and_clears_timer(monkeypatch, caplog):
    adapter = _FakeAdapter(block_stop=True)
    engine = _scan_engine(adapter)
    bus = engine._bus
    monkeypatch.setattr(engine_module, "SCAN_DBUS_TIMEOUT_SEC", 0.01)
    caplog.set_level(logging.WARNING, logger="jasper.bluetooth.engine")

    await engine.start_discovery(duration_s=0)
    expiry = engine._scan_task
    assert expiry is not None
    await expiry

    assert adapter.stop_calls == 1
    assert adapter.discovering is False
    assert engine._scan_task is None
    assert engine._bus is None
    assert bus.disconnected is True
    assert "event=bluetooth.scan_auto_stop_failed" in caplog.text
    assert "error_type=TimeoutError" in caplog.text
    assert "BlueZ StopDiscovery timed out after 0.01s" in caplog.text


@pytest.mark.asyncio
async def test_scan_manual_stop_timeout_preserves_deadline_and_propagates(
    monkeypatch,
):
    adapter = _FakeAdapter(block_stop=True)
    engine = _scan_engine(adapter)
    monkeypatch.setattr(engine_module, "SCAN_DBUS_TIMEOUT_SEC", 0.01)

    await engine.start_discovery(duration_s=0.05)
    expiry = engine._scan_task
    assert expiry is not None
    with pytest.raises(asyncio.TimeoutError, match="StopDiscovery timed out"):
        await engine.stop_discovery()
    assert not expiry.done()
    assert engine._scan_task is expiry
    assert adapter.discovering is True

    adapter.release_stop.set()
    await expiry
    assert adapter.discovering is False
    assert engine._scan_task is None


@pytest.mark.asyncio
async def test_scan_manual_stop_failure_without_deadline_releases_owner_bus(
    monkeypatch,
):
    adapter = _FakeAdapter(block_stop=True)
    adapter.discovering = True
    engine = _scan_engine(adapter)
    bus = engine._bus
    monkeypatch.setattr(engine_module, "SCAN_DBUS_TIMEOUT_SEC", 0.01)

    with pytest.raises(asyncio.TimeoutError, match="StopDiscovery timed out"):
        await engine.stop_discovery()

    assert engine._scan_task is None
    assert engine._bus is None
    assert engine._bus_recovery_required is True
    assert bus is not None and bus.disconnected is True
    assert adapter.discovering is False


@pytest.mark.asyncio
async def test_scan_recovery_failure_is_explicit_not_successful_noop(monkeypatch):
    class _BrokenConnector:
        async def connect(self):
            raise OSError("system bus unavailable")

    adapter = _FakeAdapter()
    engine = _scan_engine(adapter)
    engine._release_scan_owner_bus(engine._bus, reason="test")
    monkeypatch.setattr(
        engine_module,
        "MessageBus",
        lambda *, bus_type: _BrokenConnector(),
    )

    with pytest.raises(RuntimeError, match="BlueZ bus recovery failed"):
        await engine.start_discovery(duration_s=60)

    assert engine._bus is None
    assert engine._bus_recovery_required is True
    assert engine._scan_task is None


@pytest.mark.asyncio
async def test_device_operations_surface_shared_bus_recovery_failure(
    monkeypatch,
    caplog,
):
    class _BrokenConnector:
        async def connect(self):
            raise OSError("system bus unavailable")

    engine, _bus, _reasons = _shared_bus_engine(_FakeAdapter())
    engine._release_scan_owner_bus(engine._bus, reason="test")
    monkeypatch.setattr(
        engine_module,
        "MessageBus",
        lambda *, bus_type: _BrokenConnector(),
    )
    caplog.set_level(logging.WARNING, logger="jasper.bluetooth.engine")

    pair_events = [
        event
        async for event in engine.pair(
            "CA:AC:04:04:09:D7",
            timeout_s=1.0,
        )
    ]
    connect_result = await engine.connect("CA:AC:04:04:09:D7")
    disconnect_result = await engine.disconnect("CA:AC:04:04:09:D7")

    assert pair_events == [
        {
            "stage": "error",
            "message": (
                "Bluetooth controller recovery failed: "
                "BlueZ bus recovery failed: system bus unavailable"
            ),
        }
    ]
    expected = (
        False,
        "Bluetooth controller recovery failed: "
        "BlueZ bus recovery failed: system bus unavailable",
    )
    assert connect_result == expected
    assert disconnect_result == expected
    assert engine._bus is None
    assert engine._bus_recovery_required is True
    assert caplog.text.count("event=bluetooth.bus_recovery_failed") == 3


@pytest.mark.asyncio
async def test_scan_bus_recovery_has_a_fixed_timeout(monkeypatch):
    class _BlockingConnector:
        async def connect(self):
            await asyncio.Event().wait()

    adapter = _FakeAdapter()
    engine = _scan_engine(adapter)
    engine._release_scan_owner_bus(engine._bus, reason="test")
    monkeypatch.setattr(engine_module, "SCAN_DBUS_TIMEOUT_SEC", 0.01)
    monkeypatch.setattr(
        engine_module,
        "MessageBus",
        lambda *, bus_type: _BlockingConnector(),
    )

    with pytest.raises(asyncio.TimeoutError, match="bus recovery timed out"):
        await engine.start_discovery(duration_s=60)

    assert engine._bus is None
    assert engine._bus_recovery_required is True
    assert engine._scan_task is None


@pytest.mark.asyncio
async def test_engine_stop_cancels_scan_and_disconnects_owner_bus():
    adapter = _FakeAdapter()
    engine = _scan_engine(adapter)
    bus = engine._bus
    observer = engine._observer

    await engine.start_discovery(duration_s=60)
    expiry = engine._scan_task
    assert expiry is not None
    await engine.stop()
    await asyncio.sleep(0)

    assert expiry.done()
    assert engine._scan_task is None
    assert bus is not None and bus.disconnected is True
    assert observer.stopped is True
    assert engine._bus is None
    assert adapter.discovering is False


@pytest.mark.asyncio
async def test_engine_stop_during_start_does_not_resurrect_scan_or_timer():
    adapter = _FakeAdapter(block_start=True)
    engine = _scan_engine(adapter)
    bus = engine._bus
    observer = engine._observer

    start = asyncio.create_task(engine.start_discovery(duration_s=60))
    await adapter.start_entered.wait()
    stop = asyncio.create_task(engine.stop())
    await asyncio.sleep(0)
    assert engine._closing is True
    assert not start.done()
    assert not stop.done()

    adapter.release_start.set()
    await start
    await stop

    assert adapter.start_calls == 1
    assert adapter.stop_calls == 0
    assert adapter.discovering is False
    assert engine._scan_task is None
    assert engine._bus is None
    assert bus is not None and bus.disconnected is True
    assert observer.stopped is True


@pytest.mark.asyncio
async def test_scan_auto_stop_logs_unexpected_bluez_failure(caplog):
    adapter = _FakeAdapter(
        stop_error=DBusError("org.bluez.Error.Failed", "controller I/O failure")
    )
    engine = _scan_engine(adapter)
    caplog.set_level(logging.WARNING, logger="jasper.bluetooth.engine")

    await engine.start_discovery(duration_s=0)
    expiry = engine._scan_task
    assert expiry is not None
    await expiry

    assert adapter.discovering is False
    assert engine._scan_task is None
    assert engine._bus is None
    assert "event=bluetooth.scan_auto_stop_failed" in caplog.text
    assert "error_type=DBusError" in caplog.text
    assert "controller I/O failure" in caplog.text


@pytest.mark.asyncio
async def test_scan_start_accepts_only_exact_in_progress_error():
    adapter = _FakeAdapter(
        start_error=DBusError("org.bluez.Error.InProgress", "busy"),
    )
    engine = _scan_engine(adapter)

    await engine.start_discovery(duration_s=0)
    expiry = engine._scan_task
    assert expiry is not None
    await expiry


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_type", "detail"),
    [
        ("org.bluez.Error.Failed", "operation in progress"),
        ("org.bluez.Error.NotReady", "In Progress"),
    ],
)
async def test_scan_start_rejects_in_progress_message_on_other_error_type(
    error_type: str,
    detail: str,
):
    adapter = _FakeAdapter(start_error=DBusError(error_type, detail))
    engine = _scan_engine(adapter)

    with pytest.raises(DBusError, match=detail):
        await engine.start_discovery(duration_s=60)

    assert engine._scan_task is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_type", "detail"),
    [
        ("org.bluez.Error.NotReady", "Resource Not Ready"),
        ("org.bluez.Error.Failed", "No discovery started"),
        ("org.bluez.Error.Failed", "Adapter is not discovering"),
    ],
)
async def test_scan_stop_accepts_only_proven_already_idle_errors(
    error_type: str,
    detail: str,
):
    adapter = _FakeAdapter(stop_error=DBusError(error_type, detail))
    engine = _scan_engine(adapter)

    await engine.stop_discovery()

    assert adapter.stop_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_type", "detail"),
    [
        ("org.bluez.Error.NotAuthorized", "Operation not permitted"),
        ("org.bluez.Error.NotAuthorized", "Adapter is not discovering"),
        ("org.bluez.Error.Failed", "controller I/O failure"),
    ],
)
async def test_scan_manual_stop_propagates_permission_and_unexpected_failures(
    error_type: str,
    detail: str,
):
    adapter = _FakeAdapter(stop_error=DBusError(error_type, detail))
    engine = _scan_engine(adapter)

    with pytest.raises(DBusError, match=detail):
        await engine.stop_discovery()


@pytest.mark.parametrize(
    ("error_type", "detail", "expected"),
    [
        (
            "org.bluez.Error.AuthenticationTimeout",
            "timed out",
            "Pairing took too long. Make sure the device is in range and in pair mode, then try again.",
        ),
        (
            "org.bluez.Error.Failed",
            "org.bluez.Error.AuthenticationTimeout",
            "Pairing took too long. Make sure the device is in range and in pair mode, then try again.",
        ),
        (
            "org.bluez.Error.AuthenticationCanceled",
            "cancelled",
            "Pairing was cancelled.",
        ),
        (
            "org.bluez.Error.AuthenticationRejected",
            "rejected",
            "Pairing was rejected by the device.",
        ),
        (
            "org.bluez.Error.AuthenticationFailed",
            "failed",
            "Pairing failed. The link key didn't match.",
        ),
        (
            "org.bluez.Error.ConnectionAttemptFailed",
            "failed",
            "Could not connect. Try moving the device closer and retrying.",
        ),
        (
            "org.bluez.Error.AlreadyExists",
            "exists",
            "This device is already paired.",
        ),
        (
            "org.bluez.Error.InProgress",
            "busy",
            "Bluetooth is busy. Try again in a moment.",
        ),
        ("org.bluez.Error.Failed", "BlueZ detail", "BlueZ detail"),
        ("org.bluez.Error.Failed", "", "Unknown bluetooth error."),
    ],
)
def test_format_dbus_error_maps_known_errors_and_fallbacks(
    error_type: str,
    detail: str,
    expected: str,
):
    assert _format_dbus_error(DBusError(error_type, detail)) == expected


def test_format_dbus_error_preserves_non_dbus_message():
    assert _format_dbus_error(RuntimeError("plain failure")) == "plain failure"
