# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Best-effort duplicate checks for speaker display names.

Deliberately NOT routed through ``jasper.mdns.browse_once``. The one-shot
primitive is the right tool for "resolve a single service type into live,
addressable instances" (e.g. ``/rooms/``, the HA wizard). This module needs
the opposite shape on two axes:

  - **Names-only across MULTIPLE service types.** It browses
    ``_spotify-connect._tcp``, ``_airplay._tcp``, and ``_raop._tcp`` together
    and only cares about the instance *name* (to flag a display-name
    collision), not the address/port/TXT. ``browse_once`` is single-type and
    resolves the full record.
  - **Must include instances that don't resolve to an address.** A name
    conflict is real even if the advertising device currently has no A record
    — the friendly name still collides on the network. ``browse_once``
    intentionally DROPS address-less instances, which would hide exactly the
    conflicts this check exists to surface.

So this stays a purpose-built multi-type, name-only, resolve-optional browser.
This is a documented distinction, not un-migrated duplication.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Iterable

from .speaker_name import normalize_name

logger = logging.getLogger(__name__)


MDNS_SERVICE_TYPES = {
    "_spotify-connect._tcp.local.": "Spotify Connect",
    "_airplay._tcp.local.": "AirPlay",
    "_raop._tcp.local.": "AirPlay",
}


@dataclass(frozen=True)
class NameConflict:
    protocol: str
    name: str
    detail: str


def _key(value: str) -> str:
    return normalize_name(value).casefold()


def _strip_service_type(full_name: str, service_type: str) -> str:
    name = full_name.rstrip(".")
    suffix = "." + service_type.rstrip(".")
    if name.endswith(suffix):
        name = name[:-len(suffix)]
    return name.replace("\\032", " ")


def _display_name_candidates(full_name: str, service_type: str) -> set[str]:
    instance = _strip_service_type(full_name, service_type).strip()
    candidates = {instance} if instance else set()
    # RAOP service instances commonly look like "AABBCCDDEEFF@Living Room".
    if "@" in instance:
        candidates.add(instance.split("@", 1)[1].strip())
    return {c for c in candidates if c}


async def find_mdns_conflicts(
    requested_name: str,
    *,
    timeout: float = 2.0,
) -> list[NameConflict]:
    """Browse renderer mDNS services and return exact name matches."""
    target = _key(requested_name)
    conflicts: dict[tuple[str, str, str], NameConflict] = {}
    resolve_tasks: list[asyncio.Task] = []

    try:
        from zeroconf import IPVersion, ServiceStateChange
        from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf
    except Exception as e:  # noqa: BLE001
        logger.warning("speaker-name duplicate mdns unavailable: %s", e)
        return []

    aiozc = AsyncZeroconf(ip_version=IPVersion.V4Only)
    loop = asyncio.get_running_loop()

    async def _check(service_type: str, full_name: str) -> None:
        for candidate in _display_name_candidates(full_name, service_type):
            if _key(candidate) == target:
                protocol = MDNS_SERVICE_TYPES.get(service_type, service_type)
                conflict = NameConflict(
                    protocol=protocol,
                    name=candidate,
                    detail=full_name,
                )
                conflicts[(protocol, candidate.casefold(), full_name)] = conflict

    def _on_change(zeroconf, service_type, name, state_change):  # noqa: ANN001
        if state_change not in (ServiceStateChange.Added, ServiceStateChange.Updated):
            return

        def _schedule_check() -> None:
            resolve_tasks.append(loop.create_task(_check(service_type, name)))

        loop.call_soon_threadsafe(_schedule_check)

    browser = AsyncServiceBrowser(
        aiozc.zeroconf,
        list(MDNS_SERVICE_TYPES),
        handlers=[_on_change],
    )
    try:
        await asyncio.sleep(timeout)
        if resolve_tasks:
            await asyncio.gather(*resolve_tasks, return_exceptions=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("speaker-name duplicate mdns scan failed: %s", e)
    finally:
        try:
            await browser.async_cancel()
        finally:
            await aiozc.async_close()

    return sorted(conflicts.values(), key=lambda c: (c.protocol, c.name, c.detail))


def _variant_value(value, default=None):  # noqa: ANN001
    return getattr(value, "value", value) if value is not None else default


def _bluetooth_names_from_managed_objects(managed: dict) -> Iterable[str]:
    for ifaces in managed.values():
        props = ifaces.get("org.bluez.Device1")
        if not props:
            continue
        for key in ("Name", "Alias"):
            value = str(_variant_value(props.get(key), "") or "").strip()
            if value:
                yield value


async def find_bluetooth_conflicts(
    requested_name: str,
    *,
    timeout: float = 3.0,
    adapter: str = "hci0",
) -> list[NameConflict]:
    """Scan BlueZ's visible/cached devices for a matching friendly name.

    Bluetooth discovery is inherently best-effort: devices that are not
    discoverable during this short scan may not appear.
    """
    target = _key(requested_name)
    try:
        from dbus_next import BusType  # type: ignore
        from dbus_next.aio import MessageBus  # type: ignore
        from dbus_next.errors import DBusError  # type: ignore
    except Exception as e:  # noqa: BLE001
        logger.warning("speaker-name duplicate bluetooth unavailable: %s", e)
        return []

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        intro_root = await bus.introspect("org.bluez", "/")
        om = bus.get_proxy_object(
            "org.bluez", "/", intro_root,
        ).get_interface("org.freedesktop.DBus.ObjectManager")
        conflicts: dict[str, NameConflict] = {}

        async def _collect() -> None:
            managed = await om.call_get_managed_objects()
            for candidate in _bluetooth_names_from_managed_objects(managed):
                if _key(candidate) == target:
                    conflicts[candidate.casefold()] = NameConflict(
                        protocol="Bluetooth",
                        name=candidate,
                        detail="BlueZ device cache/discovery",
                    )

        await _collect()
        adapter_path = f"/org/bluez/{adapter}"
        try:
            intro_adapter = await bus.introspect("org.bluez", adapter_path)
            adapter_iface = bus.get_proxy_object(
                "org.bluez", adapter_path, intro_adapter,
            ).get_interface("org.bluez.Adapter1")
            try:
                await adapter_iface.call_start_discovery()
            except DBusError as e:
                if "in progress" not in str(e).lower():
                    raise
            try:
                await asyncio.sleep(timeout)
                await _collect()
            finally:
                try:
                    await adapter_iface.call_stop_discovery()
                except DBusError:
                    pass
        except Exception as e:  # noqa: BLE001
            logger.warning("speaker-name bluetooth discovery failed: %s", e)

        return sorted(conflicts.values(), key=lambda c: c.name)
    finally:
        bus.disconnect()


async def find_name_conflicts(
    requested_name: str,
    *,
    mdns_timeout: float = 2.0,
    bluetooth_timeout: float = 3.0,
) -> list[NameConflict]:
    mdns, bluetooth = await asyncio.gather(
        find_mdns_conflicts(requested_name, timeout=mdns_timeout),
        find_bluetooth_conflicts(requested_name, timeout=bluetooth_timeout),
    )
    return [*mdns, *bluetooth]
