"""Adapter-level bluez ops: power, pairing window, scan.

Thin async wrappers around `org.bluez.Adapter1`. The web layer owns
the policy ("pairing mode defaults off; auto-off after 5 min when on");
this module is the mechanism.
"""
from __future__ import annotations

import logging
from typing import Any

from dbus_next import BusType, Variant  # type: ignore
from dbus_next.aio import MessageBus  # type: ignore
from dbus_next.errors import DBusError  # type: ignore

logger = logging.getLogger(__name__)

BLUEZ_BUS = "org.bluez"
DEFAULT_ADAPTER = "hci0"

# When the user flips pairing mode ON via the web UI, auto-revert it
# OFF after this many seconds. Set on bluez via DiscoverableTimeout
# and PairableTimeout.
# This is the safety net so even if they forget to flip it back off,
# the radio closes the pairing window after a few minutes.
DISCOVERABLE_AUTO_OFF_SEC = 300


async def _close_pairing_window(props, *, best_effort: bool = False) -> None:
    """Close both BlueZ knobs that admit new pairings."""
    for key, signature, value in (
        ("Discoverable", "b", False),
        ("Pairable", "b", False),
        ("DiscoverableTimeout", "u", 0),
        ("PairableTimeout", "u", 0),
    ):
        try:
            await props.call_set(
                "org.bluez.Adapter1",
                key,
                Variant(signature, value),
            )
        except Exception as exc:  # noqa: BLE001
            if not best_effort:
                raise
            logger.warning(
                "event=bluetooth_pairing_window.rollback_failed property=%s err=%s",
                key,
                exc,
            )


async def _adapter(bus: MessageBus, adapter: str = DEFAULT_ADAPTER):
    """Build a proxy for org.bluez.Adapter1 on the given hci."""
    path = f"/org/bluez/{adapter}"
    intro = await bus.introspect(BLUEZ_BUS, path)
    obj = bus.get_proxy_object(BLUEZ_BUS, path, intro)
    return (
        obj.get_interface("org.bluez.Adapter1"),
        obj.get_interface("org.freedesktop.DBus.Properties"),
    )


async def state(adapter: str = DEFAULT_ADAPTER) -> dict[str, Any]:
    """Snapshot the adapter state: powered, discoverable, pairable,
    discovering, plus our name/alias. Returns a flat JSON-able dict.
    Raises DBusError if bluez itself is unreachable; caller decides
    whether to surface "Bluetooth daemon not running" in the UI."""
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        _, props = await _adapter(bus, adapter)
        all_props = await props.call_get_all("org.bluez.Adapter1")
        def _v(k, d=None):
            v = all_props.get(k)
            return getattr(v, "value", v) if v is not None else d
        return {
            "adapter": adapter,
            "address": _v("Address", ""),
            "alias": _v("Alias", "") or _v("Name", ""),
            "powered": bool(_v("Powered", False)),
            "discoverable": bool(_v("Discoverable", False)),
            "discoverable_timeout": int(_v("DiscoverableTimeout", 0) or 0),
            "pairable": bool(_v("Pairable", False)),
            "discovering": bool(_v("Discovering", False)),
            "uuids": [str(u) for u in (_v("UUIDs", []) or [])],
        }
    finally:
        bus.disconnect()


async def set_powered(value: bool, adapter: str = DEFAULT_ADAPTER) -> None:
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        _, props = await _adapter(bus, adapter)
        await props.call_set(
            "org.bluez.Adapter1", "Powered", Variant("b", bool(value)),
        )
    finally:
        bus.disconnect()


async def set_alias(name: str, adapter: str = DEFAULT_ADAPTER) -> None:
    """Set the adapter's friendly name as shown in Bluetooth pickers."""
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        _, props = await _adapter(bus, adapter)
        await props.call_set(
            "org.bluez.Adapter1", "Alias", Variant("s", name),
        )
    finally:
        bus.disconnect()


async def set_discoverable(
    value: bool,
    adapter: str = DEFAULT_ADAPTER,
    *,
    timeout_sec: int = DISCOVERABLE_AUTO_OFF_SEC,
) -> None:
    """Open or close the JTS pairing window.

    BlueZ separates visibility (`Discoverable`) from whether incoming
    pairing requests are accepted (`Pairable`). For JTS the UI intentionally
    treats the Discoverable toggle as "pairing mode": turning it on makes the
    speaker visible and pairable for a bounded window; turning it off closes
    both knobs. Already-paired devices can still reconnect after Pairable is
    false; BlueZ documents Pairable as affecting only incoming pairing
    requests.
    """
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        _, props = await _adapter(bus, adapter)
        if value:
            try:
                await props.call_set(
                    "org.bluez.Adapter1",
                    "PairableTimeout",
                    Variant("u", int(timeout_sec)),
                )
                await props.call_set(
                    "org.bluez.Adapter1", "Pairable", Variant("b", True),
                )
                await props.call_set(
                    "org.bluez.Adapter1",
                    "DiscoverableTimeout",
                    Variant("u", int(timeout_sec)),
                )
                await props.call_set(
                    "org.bluez.Adapter1", "Discoverable", Variant("b", True),
                )
            except Exception:
                logger.warning(
                    "event=bluetooth_pairing_window.open_failed_rollback "
                    "adapter=%s",
                    adapter,
                    exc_info=True,
                )
                await _close_pairing_window(props, best_effort=True)
                raise
        else:
            await _close_pairing_window(props)
    finally:
        bus.disconnect()


async def start_discovery(adapter: str = DEFAULT_ADAPTER) -> None:
    """Start scanning for nearby devices. Idempotent — if discovery
    is already running (e.g. a stale bluetoothctl somewhere), the
    InProgress error is swallowed."""
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        a, _ = await _adapter(bus, adapter)
        try:
            await a.call_start_discovery()
        except DBusError as e:
            if "in progress" not in str(e).lower():
                raise
    finally:
        bus.disconnect()


async def stop_discovery(adapter: str = DEFAULT_ADAPTER) -> None:
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        a, _ = await _adapter(bus, adapter)
        try:
            await a.call_stop_discovery()
        except DBusError:
            pass
    finally:
        bus.disconnect()


async def has_paired_hid(adapter: str = DEFAULT_ADAPTER) -> bool:
    """True if any currently-known device is paired AND advertises a
    HID profile (BR/EDR 0x1124 or BLE HOGP 0x1812). Used by the
    Bluetooth / Sources wizards to confirm before turning the adapter
    off while a wireless remote (e.g. the VK-01 knob) would lose its
    host. Cheap: one ObjectManager.GetManagedObjects round-trip."""
    from .models import is_hid_uuids

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        intro = await bus.introspect(BLUEZ_BUS, "/")
        om = bus.get_proxy_object(
            BLUEZ_BUS, "/", intro,
        ).get_interface("org.freedesktop.DBus.ObjectManager")
        managed = await om.call_get_managed_objects()
        for _path, ifaces in managed.items():
            dev = ifaces.get("org.bluez.Device1")
            if not dev:
                continue
            paired = dev.get("Paired")
            if paired is None or not getattr(paired, "value", paired):
                continue
            uuids_v = dev.get("UUIDs")
            uuids = getattr(uuids_v, "value", uuids_v) or []
            if is_hid_uuids([str(u) for u in uuids]):
                return True
        return False
    finally:
        bus.disconnect()


async def remove_device(
    mac: str, adapter: str = DEFAULT_ADAPTER,
) -> tuple[bool, str]:
    """Forget a paired device. Returns (ok, message)."""
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        a, _ = await _adapter(bus, adapter)
        dev_path = (
            f"/org/bluez/{adapter}/dev_{mac.upper().replace(':', '_')}"
        )
        try:
            await a.call_remove_device(dev_path)
            return True, "removed"
        except DBusError as e:
            return False, str(e)
    finally:
        bus.disconnect()
