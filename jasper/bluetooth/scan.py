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

from .models import BluetoothDevice

logger = logging.getLogger(__name__)

BLUEZ_BUS = "org.bluez"


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
        self._listeners: set[asyncio.Queue] = set()
        self._bus: MessageBus | None = None
        self._om = None
        self._unsubscribes: list[Callable] = []
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
        self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        intro = await self._bus.introspect(BLUEZ_BUS, "/")
        self._om = self._bus.get_proxy_object(
            BLUEZ_BUS, "/", intro,
        ).get_interface("org.freedesktop.DBus.ObjectManager")

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
            dev_props = ifaces.get("org.bluez.Device1")
            batt_props = ifaces.get("org.bluez.Battery1")
            if dev_props is not None:
                self._device_props[path] = dict(dev_props)
                if batt_props is not None:
                    self._battery_props[path] = dict(batt_props)
                self._devices[path] = self._build(path)
                self._broadcast("add", self._devices[path])
                asyncio.create_task(self._watch_device_props(path))
                if batt_props is not None:
                    asyncio.create_task(self._watch_battery_props(path))
            elif batt_props is not None and path in self._device_props:
                # Battery interface appeared on an existing device
                # (typical right after pairing — bluez attaches
                # Battery1 once it reads the GATT 0x180f service).
                self._battery_props[path] = dict(batt_props)
                self._devices[path] = self._build(path)
                self._broadcast("update", self._devices[path])
                asyncio.create_task(self._watch_battery_props(path))

        def _on_removed(path: str, interfaces: list[str]) -> None:
            if "org.bluez.Device1" in interfaces:
                self._device_props.pop(path, None)
                self._battery_props.pop(path, None)
                dev = self._devices.pop(path, None)
                if dev is not None:
                    self._broadcast("remove", dev)
            elif "org.bluez.Battery1" in interfaces:
                # Battery interface dropped (device disconnected).
                # Keep the device entry but clear the battery field.
                self._battery_props.pop(path, None)
                if path in self._device_props:
                    self._devices[path] = self._build(path)
                    self._broadcast("update", self._devices[path])

        self._om.on_interfaces_added(_on_added)
        self._om.on_interfaces_removed(_on_removed)
        self._unsubscribes.append(lambda: self._om.off_interfaces_added(_on_added))
        self._unsubscribes.append(lambda: self._om.off_interfaces_removed(_on_removed))

        # Subscribe to PropertiesChanged on every existing device + battery.
        for path in list(self._devices.keys()):
            asyncio.create_task(self._watch_device_props(path))
            if path in self._battery_props:
                asyncio.create_task(self._watch_battery_props(path))

    async def stop(self) -> None:
        for unsub in self._unsubscribes:
            try:
                unsub()
            except Exception:  # noqa: BLE001
                pass
        self._unsubscribes.clear()
        if self._bus is not None:
            try:
                self._bus.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._bus = None

    def _build(self, path: str) -> BluetoothDevice:
        """Construct a BluetoothDevice from the cached prop dicts."""
        return BluetoothDevice.from_props(
            path,
            self._device_props.get(path, {}),
            self._battery_props.get(path),
        )

    async def _watch_device_props(self, path: str) -> None:
        if self._bus is None:
            return
        try:
            intro = await self._bus.introspect(BLUEZ_BUS, path)
        except Exception:  # noqa: BLE001
            return
        try:
            props = self._bus.get_proxy_object(
                BLUEZ_BUS, path, intro,
            ).get_interface("org.freedesktop.DBus.Properties")
        except Exception:  # noqa: BLE001
            return

        def _on_changed(iface: str, changed: dict, invalidated: list) -> None:
            if iface != "org.bluez.Device1":
                return
            # Merge the delta into our cached Device1 props.
            cur = self._device_props.setdefault(path, {})
            cur.update(changed)
            for k in invalidated:
                cur.pop(k, None)
            new = self._build(path)
            self._devices[path] = new
            self._broadcast("update", new)

        try:
            props.on_properties_changed(_on_changed)
        except Exception:  # noqa: BLE001
            pass

    async def _watch_battery_props(self, path: str) -> None:
        """Watch org.bluez.Battery1.Percentage changes. Same shape as
        device-props watcher; updates the cached battery dict and
        rebuilds the merged BluetoothDevice."""
        if self._bus is None:
            return
        try:
            intro = await self._bus.introspect(BLUEZ_BUS, path)
        except Exception:  # noqa: BLE001
            return
        try:
            props = self._bus.get_proxy_object(
                BLUEZ_BUS, path, intro,
            ).get_interface("org.freedesktop.DBus.Properties")
        except Exception:  # noqa: BLE001
            return

        def _on_changed(iface: str, changed: dict, invalidated: list) -> None:
            if iface != "org.bluez.Battery1":
                return
            cur = self._battery_props.setdefault(path, {})
            cur.update(changed)
            for k in invalidated:
                cur.pop(k, None)
            if path in self._device_props:
                new = self._build(path)
                self._devices[path] = new
                self._broadcast("update", new)

        try:
            props.on_properties_changed(_on_changed)
        except Exception:  # noqa: BLE001
            pass

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


