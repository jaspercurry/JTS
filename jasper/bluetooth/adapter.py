# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Adapter-level bluez ops: power, pairing window, and device removal.

Thin async wrappers around `org.bluez.Adapter1`. The web layer owns
the policy ("pairing mode defaults off; auto-off after 5 min when on");
this module is the mechanism.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from dbus_next import BusType, Variant  # type: ignore
from dbus_next.aio import MessageBus  # type: ignore

from ..log_event import log_event

logger = logging.getLogger(__name__)

BLUEZ_BUS = "org.bluez"
DEFAULT_ADAPTER = "hci0"

# When the user flips pairing mode ON via the web UI, auto-revert it
# OFF after this many seconds. Set on bluez via DiscoverableTimeout
# and PairableTimeout.
# This is the safety net so even if they forget to flip it back off,
# the radio closes the pairing window after a few minutes.
DISCOVERABLE_AUTO_OFF_SEC = 300


@asynccontextmanager
async def _system_bus() -> AsyncIterator[MessageBus]:
    """Connect one system bus and always disconnect it on exit."""

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        yield bus
    finally:
        bus.disconnect()


async def _close_pairing_window(props, *, best_effort: bool = False) -> None:
    """Close every BlueZ knob that admits new pairings.

    A failure on one property must not leave the remaining admission knobs
    open.  Direct callers still receive the first error after all close
    attempts; rollback callers log each error and remain best-effort.
    """
    first_error: Exception | None = None
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
            if first_error is None:
                first_error = exc
            log_event(
                logger,
                (
                    "bluetooth_pairing_window.rollback_failed"
                    if best_effort
                    else "bluetooth_pairing_window.close_failed"
                ),
                property=key,
                err=exc,
                level=logging.WARNING,
            )
    if first_error is not None and not best_effort:
        raise first_error


async def _adapter(bus: MessageBus, adapter: str = DEFAULT_ADAPTER):
    """Build a proxy for org.bluez.Adapter1 on the given hci."""
    path = f"/org/bluez/{adapter}"
    intro = await bus.introspect(BLUEZ_BUS, path)
    obj = bus.get_proxy_object(BLUEZ_BUS, path, intro)
    return (
        obj.get_interface("org.bluez.Adapter1"),
        obj.get_interface("org.freedesktop.DBus.Properties"),
    )


class BluezSession:
    """One already-connected system bus plus the proxies built from it.

    Every helper here otherwise opens its own bus and introspects again on
    every call. A long-lived caller (the pairing agent's floor watch) pays
    that per iteration forever; handing it a session makes the steady state
    zero new connections and zero repeated introspection. The session does
    NOT own the bus — whoever connected it disconnects it.
    """

    def __init__(self, bus: MessageBus) -> None:
        self.bus = bus
        self._adapter_props: dict[str, Any] = {}
        self._object_manager: Any = None

    async def adapter_props(self, adapter: str = DEFAULT_ADAPTER) -> Any:
        props = self._adapter_props.get(adapter)
        if props is None:
            _, props = await _adapter(self.bus, adapter)
            self._adapter_props[adapter] = props
        return props

    async def object_manager(self) -> Any:
        if self._object_manager is None:
            intro = await self.bus.introspect(BLUEZ_BUS, "/")
            self._object_manager = self.bus.get_proxy_object(
                BLUEZ_BUS, "/", intro,
            ).get_interface("org.freedesktop.DBus.ObjectManager")
        return self._object_manager


@asynccontextmanager
async def _session(
    session: BluezSession | None = None,
) -> AsyncIterator[BluezSession]:
    """The caller's session, or a throwaway one for this call alone."""

    if session is not None:
        yield session
        return
    async with _system_bus() as bus:
        yield BluezSession(bus)


async def state(
    adapter: str = DEFAULT_ADAPTER,
    *,
    session: BluezSession | None = None,
) -> dict[str, Any]:
    """Snapshot the adapter state: powered, discoverable, pairable,
    discovering, plus our name/alias. Returns a flat JSON-able dict.
    Raises DBusError if bluez itself is unreachable; caller decides
    whether to surface "Bluetooth daemon not running" in the UI."""
    async with _session(session) as sess:
        props = await sess.adapter_props(adapter)
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


async def set_powered(value: bool, adapter: str = DEFAULT_ADAPTER) -> None:
    async with _system_bus() as bus:
        _, props = await _adapter(bus, adapter)
        await props.call_set(
            "org.bluez.Adapter1", "Powered", Variant("b", bool(value)),
        )


async def set_alias(name: str, adapter: str = DEFAULT_ADAPTER) -> None:
    """Set the adapter's friendly name as shown in Bluetooth pickers."""
    async with _system_bus() as bus:
        _, props = await _adapter(bus, adapter)
        await props.call_set(
            "org.bluez.Adapter1", "Alias", Variant("s", name),
        )


async def set_discoverable(
    value: bool,
    adapter: str = DEFAULT_ADAPTER,
    *,
    timeout_sec: int = DISCOVERABLE_AUTO_OFF_SEC,
    session: BluezSession | None = None,
) -> None:
    """Open or close the JTS pairing window.

    BlueZ separates visibility (`Discoverable`) from `Pairable`. For JTS the
    UI intentionally treats the Discoverable toggle as "pairing mode":
    turning it on makes the speaker visible and pairable for a bounded
    window; turning it off closes both knobs. Already-paired devices can
    still reconnect after Pairable is false.

    `Pairable` is NOT incoming-only. It maps to the kernel's bondable flag,
    which sets the Bonding bit of the SMP pairing request we send when WE
    initiate. With it off, a pair still succeeds but stores no long-term
    key, so the bond evaporates on the next disconnect. Captured on jts4
    pairing a WiiM Remote 2: ours "No bonding", the remote's "Bonding".
    Anything initiating a pair must therefore raise it first -- see
    `set_pairable`.
    """
    async with _session(session) as sess:
        props = await sess.adapter_props(adapter)
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
            except Exception:  # noqa: BLE001
                log_event(
                    logger,
                    "bluetooth_pairing_window.open_failed_rollback",
                    adapter=adapter,
                    level=logging.WARNING,
                    exc_info=True,
                )
                await _close_pairing_window(props, best_effort=True)
                raise
        else:
            await _close_pairing_window(props)


async def has_paired_hid(adapter: str = DEFAULT_ADAPTER) -> bool:
    """True if any currently-known device is paired AND advertises a
    HID profile (BR/EDR 0x1124 or BLE HOGP 0x1812). Used by the
    Bluetooth / Sources wizards to confirm before turning the adapter
    off while a wireless remote (e.g. the VK-01 knob) would lose its
    host. Cheap: one ObjectManager.GetManagedObjects round-trip."""
    from .models import is_hid_uuids

    async with _session() as sess:
        om = await sess.object_manager()
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


async def remove_device(
    mac: str, adapter: str = DEFAULT_ADAPTER,
) -> None:
    """Remove a known BlueZ device."""
    async with _system_bus() as bus:
        a, _ = await _adapter(bus, adapter)
        dev_path = (
            f"/org/bluez/{adapter}/dev_{mac.upper().replace(':', '_')}"
        )
        await a.call_remove_device(dev_path)


async def set_pairable(value: bool, adapter: str = DEFAULT_ADAPTER) -> None:
    """Raise or lower the adapter's bondable flag on its own.

    Narrower than `set_discoverable`, which also makes the speaker visible.
    An outbound pair needs the bondable flag but has no reason to advertise.
    """
    async with _system_bus() as bus:
        _, props = await _adapter(bus, adapter)
        await props.call_set(
            "org.bluez.Adapter1", "Pairable", Variant("b", bool(value)),
        )


async def untrust_unbonded(
    adapter: str = DEFAULT_ADAPTER,
    *,
    session: BluezSession | None = None,
) -> tuple[str, ...]:
    """Drop Trusted from every device on this adapter that is not Paired.

    Trust is what makes BlueZ auto-reconnect a device on every advertisement.
    Outliving the pairing is the stranding case: an unbonded HID cannot bring
    its profile up (`input-hog profile accept failed`), the reconnect repeats
    for as long as it is in range, and the device stops advertising as
    pairable -- so it can be neither used nor paired again. Granting trust
    only to a paired device is not enough on its own, because a pairing that
    never bonded disappears on the next disconnect and leaves the trust
    behind.

    `Paired` rather than `Bonded`: Paired is what BlueZ drops when an
    unbonded pairing evaporates, and it is the flag whose absence strands
    the device.

    Returns the addresses it untrusted, for the caller to log.
    """
    prefix = f"/org/bluez/{adapter}/"
    untrusted: list[str] = []
    async with _session(session) as sess:
        om = await sess.object_manager()
        managed = await om.call_get_managed_objects()
        for path, ifaces in managed.items():
            dev = ifaces.get("org.bluez.Device1")
            if not dev or not str(path).startswith(prefix):
                continue

            def _flag(key: str, props=dev) -> bool:
                raw = props.get(key)
                return bool(getattr(raw, "value", raw))

            if not _flag("Trusted") or _flag("Paired"):
                continue
            address = dev.get("Address")
            address = str(getattr(address, "value", address) or path)
            try:
                dev_intro = await sess.bus.introspect(BLUEZ_BUS, path)
                props = sess.bus.get_proxy_object(
                    BLUEZ_BUS, path, dev_intro,
                ).get_interface("org.freedesktop.DBus.Properties")
                await props.call_set(
                    "org.bluez.Device1", "Trusted", Variant("b", False),
                )
            except Exception as exc:  # noqa: BLE001
                # A device BlueZ pruned between the listing and this write is
                # the common case, and it is already the outcome we wanted.
                # One unreachable device must not abandon the rest of the sweep.
                log_event(
                    logger,
                    "bluetooth.untrust_unbonded_skipped",
                    address=address,
                    err=repr(exc),
                    level=logging.WARNING,
                )
                continue
            untrusted.append(address)
    return tuple(untrusted)
