# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Live device-list observer.

Subscribes to bluez's ObjectManager and Device1 PropertiesChanged signals
and exposes them as an async iterator of `(action, device)` tuples. The
web layer turns these into SSE events.

`action` is one of: "add" (device newly seen), "update" (existing
device's properties changed — RSSI, Connected, Paired), "remove"
(device dropped from cache).

The observer maintains the full device list in memory so it can
materialise an initial snapshot for clients that subscribe mid-flight.
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Callable

from dbus_next import BusType  # type: ignore
from dbus_next.aio import MessageBus  # type: ignore
from dbus_next.errors import DBusError  # type: ignore

from .models import BluetoothDevice, UUID_BATTERY_LEVEL

logger = logging.getLogger(__name__)

BLUEZ_BUS = "org.bluez"
BLUEZ_GATT_CHARACTERISTIC_IFACE = "org.bluez.GattCharacteristic1"


def _variant_value(value):
    return getattr(value, "value", value)


def _battery_level_characteristic_path(
    managed_objects: dict,
    device_path: str,
) -> str | None:
    """Return the standard Battery Level characteristic path, if present."""
    prefix = f"{device_path}/"
    for path, ifaces in managed_objects.items():
        if not str(path).startswith(prefix):
            continue
        char_props = ifaces.get(BLUEZ_GATT_CHARACTERISTIC_IFACE)
        if char_props is None:
            continue
        uuid = str(_variant_value(char_props.get("UUID")) or "").lower()
        if UUID_BATTERY_LEVEL in uuid:
            return str(path)
    return None


def _battery_percent_from_read_value(value) -> int | None:
    """Decode a BLE Battery Level characteristic ReadValue result."""
    if not value:
        return None
    try:
        pct = int(value[0])
    except (TypeError, ValueError, IndexError):
        return None
    if 0 <= pct <= 100:
        return pct
    return None


class DeviceObserver:
    """Maintains a live list of `BluetoothDevice`s. Used by the web
    layer to stream updates and by the engine to look up devices by
    path or MAC.

    One instance per process. The web server keeps it running for
    the lifetime of the daemon; per-request streams hook into its
    event queue.

    Tracks both `org.bluez.Device1` properties and `org.bluez.Battery1`
    properties (when present) so the merged BluetoothDevice carries
    a battery percentage. Battery is a separate D-Bus interface that
    can appear after a device pairs; we subscribe to InterfacesAdded
    to catch it.
    """

    def __init__(self) -> None:
        self._devices: dict[str, BluetoothDevice] = {}
        # Cached raw prop dicts so PropertiesChanged deltas can rebuild
        # the BluetoothDevice without losing any field. Keyed by
        # device D-Bus path.
        self._device_props: dict[str, dict] = {}
        self._battery_props: dict[str, dict] = {}
        self._battery_read_tasks: dict[str, asyncio.Task] = {}
        self._listeners: set[asyncio.Queue] = set()
        self._bus: MessageBus | None = None
        self._om = None
        self._unsubscribes: list[Callable[[], None]] = []
        self._device_unsubscribes: dict[str, Callable[[], None]] = {}
        self._battery_unsubscribes: dict[str, Callable[[], None]] = {}
        self._watch_tasks: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()

    @property
    def devices(self) -> list[BluetoothDevice]:
        return list(self._devices.values())

    def get(self, path: str) -> BluetoothDevice | None:
        return self._devices.get(path)

    def get_by_mac(self, mac: str) -> BluetoothDevice | None:
        mac_norm = mac.upper()
        for d in self._devices.values():
            if d.address.upper() == mac_norm:
                return d
        return None

    async def start(self) -> None:
        """Connect to the system bus, subscribe to bluez signals,
        snapshot the current devices into memory."""
        if self._bus is not None:
            return
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        self._bus = bus
        intro = await bus.introspect(BLUEZ_BUS, "/")
        self._om = bus.get_proxy_object(
            BLUEZ_BUS,
            "/",
            intro,
        ).get_interface("org.freedesktop.DBus.ObjectManager")
        om = self._om

        # Snapshot existing devices + battery interfaces.
        managed = await self._om.call_get_managed_objects()
        for path, ifaces in managed.items():
            dev_props = ifaces.get("org.bluez.Device1")
            if dev_props is None:
                continue
            self._device_props[path] = dict(dev_props)
            batt_props = ifaces.get("org.bluez.Battery1")
            if batt_props is not None:
                self._battery_props[path] = dict(batt_props)
            self._devices[path] = self._build(path)

        # Live signals.
        def _on_added(path: str, ifaces: dict) -> None:
            if self._bus is not bus or self._om is not om:
                return
            dev_props = ifaces.get("org.bluez.Device1")
            batt_props = ifaces.get("org.bluez.Battery1")
            if dev_props is not None:
                self._unsubscribe_path(self._device_unsubscribes, path)
                self._unsubscribe_path(self._battery_unsubscribes, path)
                battery_task = self._battery_read_tasks.pop(path, None)
                if battery_task is not None and not battery_task.done():
                    battery_task.cancel()
                self._device_props[path] = dict(dev_props)
                if batt_props is not None:
                    self._battery_props[path] = dict(batt_props)
                else:
                    # A fresh Device1 instance must not inherit Battery1 or
                    # direct-GATT data cached for an older instance at the
                    # same BlueZ path.
                    self._battery_props.pop(path, None)
                self._devices[path] = self._build(path)
                self._broadcast("add", self._devices[path])
                task = asyncio.create_task(self._watch_device_props(path))
                self._watch_tasks.add(task)
                task.add_done_callback(self._watch_tasks.discard)
                self._schedule_battery_refresh(path)
                if batt_props is not None:
                    task = asyncio.create_task(self._watch_battery_props(path))
                    self._watch_tasks.add(task)
                    task.add_done_callback(self._watch_tasks.discard)
            elif batt_props is not None and path in self._device_props:
                # Battery interface appeared on an existing device
                # (typical right after pairing — bluez attaches
                # Battery1 once it reads the GATT 0x180f service).
                self._unsubscribe_path(self._battery_unsubscribes, path)
                battery_task = self._battery_read_tasks.pop(path, None)
                if battery_task is not None and not battery_task.done():
                    battery_task.cancel()
                self._battery_props[path] = dict(batt_props)
                self._devices[path] = self._build(path)
                self._broadcast("update", self._devices[path])
                task = asyncio.create_task(self._watch_battery_props(path))
                self._watch_tasks.add(task)
                task.add_done_callback(self._watch_tasks.discard)
                self._schedule_battery_refresh(path)

        def _on_removed(path: str, interfaces: list[str]) -> None:
            if self._bus is not bus or self._om is not om:
                return
            if "org.bluez.Device1" in interfaces:
                self._unsubscribe_path(self._device_unsubscribes, path)
                self._unsubscribe_path(self._battery_unsubscribes, path)
                battery_task = self._battery_read_tasks.pop(path, None)
                if battery_task is not None and not battery_task.done():
                    battery_task.cancel()
                self._device_props.pop(path, None)
                self._battery_props.pop(path, None)
                dev = self._devices.pop(path, None)
                if dev is not None:
                    self._broadcast("remove", dev)
            elif "org.bluez.Battery1" in interfaces:
                # Battery interface dropped (device disconnected).
                # Keep the device entry but clear the battery field.
                self._unsubscribe_path(self._battery_unsubscribes, path)
                battery_task = self._battery_read_tasks.pop(path, None)
                if battery_task is not None and not battery_task.done():
                    battery_task.cancel()
                self._battery_props.pop(path, None)
                if path in self._device_props:
                    self._devices[path] = self._build(path)
                    self._broadcast("update", self._devices[path])

        self._om.on_interfaces_added(_on_added)
        self._om.on_interfaces_removed(_on_removed)
        self._unsubscribes.append(lambda: om.off_interfaces_added(_on_added))
        self._unsubscribes.append(lambda: om.off_interfaces_removed(_on_removed))

        # Subscribe to PropertiesChanged on every existing device + battery.
        for path in list(self._devices.keys()):
            task = asyncio.create_task(self._watch_device_props(path))
            self._watch_tasks.add(task)
            task.add_done_callback(self._watch_tasks.discard)
            if path in self._battery_props:
                task = asyncio.create_task(self._watch_battery_props(path))
                self._watch_tasks.add(task)
                task.add_done_callback(self._watch_tasks.discard)
            self._schedule_battery_refresh(path)

    async def stop(self) -> None:
        bus = self._bus
        self._bus = None
        self._om = None
        for path in list(self._device_unsubscribes):
            self._unsubscribe_path(self._device_unsubscribes, path)
        for path in list(self._battery_unsubscribes):
            self._unsubscribe_path(self._battery_unsubscribes, path)
        tasks = [*self._battery_read_tasks.values(), *self._watch_tasks]
        for task in tasks:
            task.cancel()
        self._battery_read_tasks.clear()
        self._watch_tasks.clear()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for unsub in self._unsubscribes:
            try:
                unsub()
            except Exception:  # noqa: BLE001
                pass
        self._unsubscribes.clear()
        if bus is not None:
            try:
                bus.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self._devices.clear()
        self._device_props.clear()
        self._battery_props.clear()

    @staticmethod
    def _unsubscribe_path(
        callbacks: dict[str, Callable[[], None]],
        path: str,
    ) -> None:
        unsubscribe = callbacks.pop(path, None)
        if unsubscribe is None:
            return
        try:
            unsubscribe()
        except Exception:  # noqa: BLE001
            pass

    def _build(self, path: str) -> BluetoothDevice:
        """Construct a BluetoothDevice from the cached prop dicts."""
        return BluetoothDevice.from_props(
            path,
            self._device_props.get(path, {}),
            self._battery_props.get(path),
        )

    async def _watch_device_props(self, path: str) -> None:
        bus = self._bus
        live_props = self._device_props.get(path)
        if bus is None or live_props is None:
            return
        try:
            intro = await bus.introspect(BLUEZ_BUS, path)
        except Exception:  # noqa: BLE001
            return
        try:
            props = bus.get_proxy_object(
                BLUEZ_BUS,
                path,
                intro,
            ).get_interface("org.freedesktop.DBus.Properties")
        except Exception:  # noqa: BLE001
            return
        if self._bus is not bus or self._device_props.get(path) is not live_props:
            return

        def _on_changed(iface: str, changed: dict, invalidated: list) -> None:
            if (
                iface != "org.bluez.Device1"
                or self._bus is not bus
                or self._device_props.get(path) is not live_props
            ):
                return
            # Merge the delta into our cached Device1 props.
            live_props.update(changed)
            for k in invalidated:
                live_props.pop(k, None)
            new = self._build(path)
            self._devices[path] = new
            self._broadcast("update", new)
            self._schedule_battery_refresh(path)

        try:
            self._unsubscribe_path(self._device_unsubscribes, path)
            props.on_properties_changed(_on_changed)
        except Exception:  # noqa: BLE001
            return
        self._device_unsubscribes[path] = lambda: props.off_properties_changed(
            _on_changed
        )

    async def _watch_battery_props(self, path: str) -> None:
        """Watch org.bluez.Battery1.Percentage changes. Same shape as
        device-props watcher; updates the cached battery dict and
        rebuilds the merged BluetoothDevice."""
        bus = self._bus
        live_device_props = self._device_props.get(path)
        live_battery_props = self._battery_props.get(path)
        if bus is None or live_device_props is None or live_battery_props is None:
            return
        try:
            intro = await bus.introspect(BLUEZ_BUS, path)
        except Exception:  # noqa: BLE001
            return
        try:
            props = bus.get_proxy_object(
                BLUEZ_BUS,
                path,
                intro,
            ).get_interface("org.freedesktop.DBus.Properties")
        except Exception:  # noqa: BLE001
            return
        if (
            self._bus is not bus
            or self._device_props.get(path) is not live_device_props
            or self._battery_props.get(path) is not live_battery_props
        ):
            return

        def _on_changed(iface: str, changed: dict, invalidated: list) -> None:
            if (
                iface != "org.bluez.Battery1"
                or self._bus is not bus
                or self._device_props.get(path) is not live_device_props
                or self._battery_props.get(path) is not live_battery_props
            ):
                return
            live_battery_props.update(changed)
            for k in invalidated:
                live_battery_props.pop(k, None)
            new = self._build(path)
            self._devices[path] = new
            self._broadcast("update", new)
            self._schedule_battery_refresh(path)

        try:
            self._unsubscribe_path(self._battery_unsubscribes, path)
            props.on_properties_changed(_on_changed)
        except Exception:  # noqa: BLE001
            return
        self._battery_unsubscribes[path] = lambda: props.off_properties_changed(
            _on_changed
        )

    def _schedule_battery_refresh(self, path: str) -> None:
        if self._bus is None or self._om is None or path not in self._device_props:
            return
        device = self._build(path)
        if not (
            device.connected and device.services_resolved and device.battery_capable
        ):
            return
        existing = self._battery_read_tasks.get(path)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(self._refresh_battery_from_gatt(path))
        self._battery_read_tasks[path] = task

        def _forget(completed: asyncio.Task, p: str = path) -> None:
            if self._battery_read_tasks.get(p) is completed:
                self._battery_read_tasks.pop(p, None)

        task.add_done_callback(_forget)

    async def _refresh_battery_from_gatt(self, path: str) -> None:
        """Directly read BLE Battery Level.

        BlueZ normally mirrors the standard Battery Service to
        org.bluez.Battery1.Percentage, but some BLE HID devices leave
        that convenience property stale while the characteristic itself
        has the correct value. Reading 0x2a19 keeps the UI honest
        without changing pairing/control semantics.
        """
        bus = self._bus
        om = self._om
        live_device_props = self._device_props.get(path)
        live_battery_props = self._battery_props.get(path)
        if bus is None or om is None or live_device_props is None:
            return
        try:
            managed = await om.call_get_managed_objects()
            char_path = _battery_level_characteristic_path(managed, path)
            if char_path is None:
                return
            intro = await bus.introspect(BLUEZ_BUS, char_path)
            char = bus.get_proxy_object(
                BLUEZ_BUS,
                char_path,
                intro,
            ).get_interface(BLUEZ_GATT_CHARACTERISTIC_IFACE)
            pct = _battery_percent_from_read_value(
                await char.call_read_value({}),
            )
        except (AttributeError, DBusError, KeyError, RuntimeError, TypeError) as exc:
            logger.debug(
                "bluetooth direct battery read failed for %s: %s",
                path,
                exc,
            )
            return
        if pct is None:
            return
        if (
            self._bus is not bus
            or self._om is not om
            or self._device_props.get(path) is not live_device_props
            or self._battery_props.get(path) is not live_battery_props
        ):
            return
        cur = self._battery_props.setdefault(path, {})
        old = _variant_value(cur.get("Percentage"))
        cur["Percentage"] = pct
        cur.setdefault("Source", "GATT Battery Service")
        new = self._build(path)
        self._devices[path] = new
        if old != pct:
            self._broadcast("update", new)

    def _broadcast(self, action: str, device: BluetoothDevice) -> None:
        for q in list(self._listeners):
            try:
                q.put_nowait((action, device))
            except asyncio.QueueFull:
                pass

    async def subscribe(self) -> "_Subscription":
        """Create a new event subscription. Yields the current
        snapshot first, then live events. Caller must call .close()
        when done (use as a context manager)."""
        q: asyncio.Queue = asyncio.Queue(maxsize=1024)
        self._listeners.add(q)
        # Seed with the current snapshot.
        for d in self._devices.values():
            q.put_nowait(("add", d))
        return _Subscription(self, q)


class _Subscription:
    def __init__(self, observer: DeviceObserver, q: asyncio.Queue):
        self._observer = observer
        self._q = q
        self._closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._observer._listeners.discard(self._q)

    async def events(self) -> AsyncIterator[tuple[str, BluetoothDevice]]:
        while not self._closed:
            try:
                yield await self._q.get()
            except asyncio.CancelledError:
                self.close()
                raise
