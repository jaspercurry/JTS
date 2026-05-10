"""Bluez D-Bus helpers for the web-driven HID accessory pairing flow.

Drives `org.bluez` via dbus-next (asyncio-native) so the /dial/ web
wizard can:

  1. Put the Pi's BT adapter into discoverable + discovering state.
  2. Register an "always agree" Agent (NoInputNoOutput — HID
     accessories like the VK-01 have no keyboard or display, so we
     accept whatever they advertise via Just-Works pairing).
  3. Watch for new device interfaces. Filter to HID class-of-device.
  4. Pair → Trust → Connect.

`pair_first_hid()` is an async generator: it yields status events as
the flow progresses (`scanning`, `found`, `pairing`, `trusted`,
`connected`, `error`) so the wizard can stream them over SSE without
the caller having to know anything about BlueZ.

This module assumes Trixie's stock bluez 5.x and the dbus-next 0.2+
API. Tested against the system bus, not session — bluez doesn't
expose itself on the session bus.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import AsyncIterator

from dbus_next import BusType, Variant  # type: ignore
from dbus_next.aio import MessageBus  # type: ignore
from dbus_next.errors import DBusError  # type: ignore
from dbus_next.service import ServiceInterface, method  # type: ignore

logger = logging.getLogger(__name__)


BLUEZ_BUS = "org.bluez"
BLUEZ_OM_PATH = "/"

# Class-of-Device value(s) we treat as "HID accessory". The BT spec
# splits CoD into Major Service Class | Major Device Class | Minor
# Device Class | Format Type bits. Anything in the 0x002500..0x0025FF
# range is "Peripheral" major class — keyboards / mice / remotes /
# knobs. We also accept 0x000540 (sub-keyboard cited by Anticater
# listings) and bail-out-true if name matches a known accessory.
HID_PERIPHERAL_MAJOR = 0x0500  # bits 8..12 = "Peripheral"


def _is_hid_peripheral(class_of_device: int) -> bool:
    """Major device class == Peripheral (0x05). Covers keyboards,
    mice, joysticks, remotes, knobs — the things our bridge can
    actually use once paired."""
    major = (class_of_device >> 8) & 0x1F
    return major == 0x05


class _Agent(ServiceInterface):
    """Bluez agent with NoInputNoOutput capability. All confirmation
    callbacks accept silently (Just Works pairing).

    Bluez requires an agent registered for any pairing flow. The agent
    is keyed by an object path we own; we register it as the default
    agent for the duration of `pair_first_hid()` and unregister at
    the end.

    The trade-off vs. the "DisplayYesNo" capability: NoInputNoOutput
    can pair with any device without a button press to confirm a
    passkey, which is the right behaviour for headless HID accessories
    that have no display and no keyboard. The cost is that BT classic
    SSP downgrades to "Just Works" — any device in range during the
    flow could theoretically pair. That's acceptable here because:
      (a) the flow is short-lived (~10 s typical),
      (b) the user has to put their specific device in pair mode
          (physical button press), AND
      (c) only one device can typically be in pair mode at once.
    """

    def __init__(self) -> None:
        super().__init__("org.bluez.Agent1")

    # dbus-next reads each method's type annotations to derive the
    # D-Bus signature (`"o"` = object path, `"s"` = string, `"u"` =
    # uint32, `"q"` = uint16). Methods that don't return a value MUST
    # NOT have a `-> None` annotation — dbus-next rejects that with
    # "service annotations must be a string constant (got None)".
    # Either omit the return annotation entirely, or use an empty
    # `-> ""` to signal void explicitly.

    @method()
    def Release(self):  # noqa: N802 (D-Bus method names are CamelCase)
        pass

    @method()
    def RequestPinCode(self, device: "o") -> "s":  # type: ignore  # noqa: N802
        # No keyboard — bluez should never call this for NoInputNoOutput,
        # but if it does, return a default that bluez will reject.
        logger.warning("agent: RequestPinCode(%s) — unexpected", device)
        return "0000"

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s"):  # type: ignore  # noqa: N802
        logger.debug("agent: DisplayPinCode(%s, %s)", device, pincode)

    @method()
    def RequestPasskey(self, device: "o") -> "u":  # type: ignore  # noqa: N802
        logger.warning("agent: RequestPasskey(%s) — unexpected", device)
        return 0

    @method()
    def DisplayPasskey(  # noqa: N802
        self, device: "o", passkey: "u", entered: "q",  # type: ignore
    ):
        logger.debug(
            "agent: DisplayPasskey(%s, %d, %d)", device, passkey, entered,
        )

    @method()
    def RequestConfirmation(  # noqa: N802
        self, device: "o", passkey: "u",  # type: ignore
    ):
        # Accept silently.
        logger.info("agent: confirming passkey %06d for %s", passkey, device)

    @method()
    def RequestAuthorization(self, device: "o"):  # type: ignore  # noqa: N802
        logger.info("agent: authorizing %s", device)

    @method()
    def AuthorizeService(  # noqa: N802
        self, device: "o", uuid: "s",  # type: ignore
    ):
        logger.info("agent: authorizing %s service %s", device, uuid)

    @method()
    def Cancel(self):  # noqa: N802
        logger.info("agent: Cancel()")


def _v(obj: dict[str, Variant], key: str, default=None):
    """Unpack a Variant-of-X dict (the InterfacesAdded payload format)."""
    val = obj.get(key)
    return val.value if val is not None else default


async def pair_first_hid(
    *,
    name_regex: str | None = None,
    timeout_s: float = 60.0,
    adapter: str = "hci0",
    agent_path: str = "/com/jasper/agent",
) -> AsyncIterator[dict]:
    """Drive bluez to pair the first HID peripheral discovered while
    the user has their device in pair mode. Async generator yielding
    status dicts:

      {"status": "scanning"}
      {"status": "found", "name": ..., "mac": ..., "path": ...}
      {"status": "pairing", "name": ..., "mac": ...}
      {"status": "trusted", "name": ..., "mac": ...}
      {"status": "connected", "name": ..., "mac": ...}
      {"status": "error", "message": ...}

    `name_regex` filters discovered devices by their advertised name
    (re.search). Without it, the flow accepts any HID-class device in
    pair mode — including stray nearby Apple Magic Mouse / Surface
    Dials. Callers know what kind of accessory the user is pairing,
    so they should pass the registered regex (see registry.py's
    `bt_name_regex`).

    The web layer turns each yielded dict into an SSE event. The flow
    stops on first error or first successful connect. Adapter is left
    in non-discoverable / non-discovering state at exit.
    """
    name_re = re.compile(name_regex) if name_regex else None
    bus = None
    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    except Exception as e:  # noqa: BLE001
        yield {"status": "error", "message": f"can't connect to system bus: {e}"}
        return

    try:
        # Register the always-agree agent.
        agent = _Agent()
        bus.export(agent_path, agent)
        try:
            mgr_intro = await bus.introspect(BLUEZ_BUS, "/org/bluez")
        except DBusError as e:
            yield {"status": "error",
                   "message": f"bluez not running on system bus: {e}"}
            return
        mgr = bus.get_proxy_object(
            BLUEZ_BUS, "/org/bluez", mgr_intro,
        ).get_interface("org.bluez.AgentManager1")
        try:
            await mgr.call_register_agent(agent_path, "NoInputNoOutput")
        except DBusError as e:
            msg = str(e).lower()
            if "already exists" not in msg:
                yield {"status": "error",
                       "message": f"agent register failed: {e}"}
                return
        try:
            await mgr.call_request_default_agent(agent_path)
        except DBusError as e:
            logger.debug("agent: request_default_agent declined: %s", e)

        # Power on the adapter and start discovery.
        adapter_path = f"/org/bluez/{adapter}"
        try:
            adapter_intro = await bus.introspect(BLUEZ_BUS, adapter_path)
        except DBusError as e:
            yield {"status": "error",
                   "message": f"adapter {adapter} not found: {e}"}
            return
        adapter_obj = bus.get_proxy_object(
            BLUEZ_BUS, adapter_path, adapter_intro,
        )
        adapter_iface = adapter_obj.get_interface("org.bluez.Adapter1")
        adapter_props = adapter_obj.get_interface(
            "org.freedesktop.DBus.Properties",
        )
        try:
            await adapter_props.call_set(
                "org.bluez.Adapter1", "Powered", Variant("b", True),
            )
        except DBusError as e:
            yield {"status": "error",
                   "message": f"can't power adapter: {e}"}
            return
        try:
            await adapter_iface.call_start_discovery()
        except DBusError as e:
            msg = str(e).lower()
            if "in progress" not in msg:
                yield {"status": "error",
                       "message": f"can't start discovery: {e}"}
                return

        yield {"status": "scanning"}

        # Watch for new device interfaces.
        om_intro = await bus.introspect(BLUEZ_BUS, BLUEZ_OM_PATH)
        om = bus.get_proxy_object(
            BLUEZ_BUS, BLUEZ_OM_PATH, om_intro,
        ).get_interface("org.freedesktop.DBus.ObjectManager")

        found_queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()

        def _on_interfaces_added(path: str, ifaces: dict) -> None:
            dev = ifaces.get("org.bluez.Device1")
            if dev is None:
                return
            found_queue.put_nowait((path, dev))

        om.on_interfaces_added(_on_interfaces_added)

        # Seed the queue with any devices the adapter has already
        # cached from a previous scan (the InterfacesAdded signal only
        # fires for *new* discoveries this session). Skip the ones
        # already paired — they aren't candidates.
        try:
            managed = await om.call_get_managed_objects()
            for path, ifaces in managed.items():
                dev = ifaces.get("org.bluez.Device1")
                if dev is None:
                    continue
                if _v(dev, "Paired") is True:
                    continue
                # Only seed devices that are currently discoverable —
                # otherwise we'd flood the queue with every device the
                # adapter has ever seen.
                if _v(dev, "RSSI") is None:
                    continue
                found_queue.put_nowait((path, dev))
        except DBusError:
            pass

        # Wait for an HID match. Time-bounded.
        deadline = time.monotonic() + timeout_s
        target_path: str | None = None
        target_name = ""
        target_mac = ""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                yield {"status": "error",
                       "message": "no HID accessory appeared in pair mode within "
                                  f"{int(timeout_s)} s — make sure you held the "
                                  "knob's button long enough"}
                return
            try:
                path, dev = await asyncio.wait_for(
                    found_queue.get(), timeout=remaining,
                )
            except asyncio.TimeoutError:
                continue
            cod = _v(dev, "Class", 0)
            name = _v(dev, "Name", _v(dev, "Alias", "")) or ""
            mac = _v(dev, "Address", "") or ""
            paired = _v(dev, "Paired", False)
            if paired:
                continue
            if cod and not _is_hid_peripheral(cod):
                continue
            if name_re is not None and not name_re.search(name):
                logger.debug(
                    "pair: skipping %s (%s) — doesn't match name filter",
                    name, mac,
                )
                continue
            target_path = path
            target_name = name or "(unnamed)"
            target_mac = mac
            yield {
                "status": "found",
                "name": target_name,
                "mac": target_mac,
                "path": target_path,
            }
            break

        # Stop discovery while we pair — keeps the radio quieter and
        # avoids bluez churn during the pair handshake.
        try:
            await adapter_iface.call_stop_discovery()
        except DBusError:
            pass

        # Pair.
        dev_intro = await bus.introspect(BLUEZ_BUS, target_path)
        dev_obj = bus.get_proxy_object(BLUEZ_BUS, target_path, dev_intro)
        dev_iface = dev_obj.get_interface("org.bluez.Device1")
        dev_props = dev_obj.get_interface("org.freedesktop.DBus.Properties")

        yield {"status": "pairing", "name": target_name, "mac": target_mac}
        try:
            await dev_iface.call_pair()
        except DBusError as e:
            yield {"status": "error",
                   "message": f"pair failed: {e}",
                   "name": target_name, "mac": target_mac}
            return

        # Trust so subsequent connections don't prompt the agent.
        try:
            await dev_props.call_set(
                "org.bluez.Device1", "Trusted", Variant("b", True),
            )
        except DBusError as e:
            logger.warning("trust set failed (proceeding): %s", e)
        yield {"status": "trusted", "name": target_name, "mac": target_mac}

        # Connect (HID profile).
        try:
            await dev_iface.call_connect()
        except DBusError as e:
            yield {"status": "error",
                   "message": f"connect failed: {e}",
                   "name": target_name, "mac": target_mac}
            return
        yield {"status": "connected",
               "name": target_name, "mac": target_mac, "path": target_path}

    finally:
        # Best-effort cleanup. Don't let cleanup raise into the caller.
        try:
            if bus is not None:
                bus.unexport(agent_path)
        except Exception:  # noqa: BLE001
            pass
        try:
            if bus is not None:
                try:
                    adapter_path = f"/org/bluez/{adapter}"
                    intro = await bus.introspect(BLUEZ_BUS, adapter_path)
                    ai = bus.get_proxy_object(
                        BLUEZ_BUS, adapter_path, intro,
                    ).get_interface("org.bluez.Adapter1")
                    await ai.call_stop_discovery()
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass
        try:
            if bus is not None:
                bus.disconnect()
        except Exception:  # noqa: BLE001
            pass


async def list_paired_hids(*, adapter: str = "hci0") -> list[dict]:
    """Return paired HID devices on the adapter.

    Each entry: {name, mac, path, connected, battery (or None)}.
    Battery is read from `org.bluez.Battery1.Percentage` if the device
    exposes it (HID Battery System usage page on standard-compliant
    accessories). Returns [] on bluez errors.
    """
    bus = None
    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        om_intro = await bus.introspect(BLUEZ_BUS, BLUEZ_OM_PATH)
        om = bus.get_proxy_object(
            BLUEZ_BUS, BLUEZ_OM_PATH, om_intro,
        ).get_interface("org.freedesktop.DBus.ObjectManager")
        managed = await om.call_get_managed_objects()
    except Exception:  # noqa: BLE001
        if bus is not None:
            try:
                bus.disconnect()
            except Exception:  # noqa: BLE001
                pass
        return []
    out: list[dict] = []
    try:
        for path, ifaces in managed.items():
            dev = ifaces.get("org.bluez.Device1")
            if dev is None:
                continue
            if not _v(dev, "Paired"):
                continue
            cod = _v(dev, "Class", 0)
            if cod and not _is_hid_peripheral(cod):
                # Class-of-Device is sometimes 0 until the device fully
                # advertises; we keep the entry only if CoD is unset
                # (defer to name matching at the UI layer) OR if it
                # matches the HID major.
                if cod != 0:
                    continue
            battery = None
            batt = ifaces.get("org.bluez.Battery1")
            if batt is not None:
                battery = _v(batt, "Percentage")
            out.append({
                "name": _v(dev, "Name", _v(dev, "Alias", "")) or "(unnamed)",
                "mac": _v(dev, "Address", "") or "",
                "path": path,
                "connected": bool(_v(dev, "Connected", False)),
                "battery": battery,
            })
    finally:
        try:
            bus.disconnect()
        except Exception:  # noqa: BLE001
            pass
    return out


async def forget(mac: str, *, adapter: str = "hci0") -> tuple[bool, str]:
    """Remove a paired BT device. Returns (ok, message)."""
    bus = None
    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        adapter_path = f"/org/bluez/{adapter}"
        intro = await bus.introspect(BLUEZ_BUS, adapter_path)
        ai = bus.get_proxy_object(
            BLUEZ_BUS, adapter_path, intro,
        ).get_interface("org.bluez.Adapter1")
        # bluez object path uses underscores in MAC.
        dev_path = f"{adapter_path}/dev_{mac.upper().replace(':', '_')}"
        await ai.call_remove_device(dev_path)
        return True, "removed"
    except DBusError as e:
        return False, str(e)
    except Exception as e:  # noqa: BLE001
        return False, f"unexpected error: {e}"
    finally:
        if bus is not None:
            try:
                bus.disconnect()
            except Exception:  # noqa: BLE001
                pass
