"""Adapter-level bluez ops: power, discoverable, pairable, scan.

Thin async wrappers around `org.bluez.Adapter1`. The web layer owns
the policy ("Discoverable defaults off; auto-off after 5 min when on");
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

# When the user flips Discoverable ON via the web UI, auto-revert it
# OFF after this many seconds. Set on bluez via DiscoverableTimeout.
# The user said "we don't want the speaker just always broadcasting" —
# this is the safety net so even if they forget to flip it back off,
# the radio quiets down after a few minutes.
DISCOVERABLE_AUTO_OFF_SEC = 300


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


async def set_discoverable(
    value: bool,
    adapter: str = DEFAULT_ADAPTER,
    *,
    timeout_sec: int = DISCOVERABLE_AUTO_OFF_SEC,
) -> None:
    """Set the adapter's Discoverable property. When turning ON,
    also set DiscoverableTimeout so the radio quiets down even if
    the user forgets to flip the toggle back. When turning OFF,
    set timeout back to 0 (the bluez default for the off state)."""
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        _, props = await _adapter(bus, adapter)
        if value:
            await props.call_set(
                "org.bluez.Adapter1",
                "DiscoverableTimeout",
                Variant("u", int(timeout_sec)),
            )
            await props.call_set(
                "org.bluez.Adapter1", "Discoverable", Variant("b", True),
            )
        else:
            await props.call_set(
                "org.bluez.Adapter1", "Discoverable", Variant("b", False),
            )
            # Reset timeout for a clean state.
            await props.call_set(
                "org.bluez.Adapter1",
                "DiscoverableTimeout", Variant("u", 0),
            )
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
