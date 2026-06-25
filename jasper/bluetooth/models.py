# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Data shapes shared across the bluetooth package."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# Common org.bluez UUIDs we recognise for icon/handler hints. Not
# exhaustive — anything not in here falls into the "Generic" bucket
# and the default handler. Keep these as fragments for substring
# matching on the full 128-bit UUIDs that bluez reports.
UUID_A2DP_SINK = "0000110b-"     # Audio Sink (Pi-as-speaker source)
UUID_A2DP_SOURCE = "0000110a-"   # Audio Source (rare for us)
UUID_HFP_HF = "0000111e-"        # Hands-Free
UUID_AVRCP = "0000110e-"         # Audio/Video Remote Control
UUID_HID = "00001124-"           # Human Interface Device (BR/EDR HID)
UUID_HOGP = "00001812-"          # HID over GATT (BLE HID)
UUID_BATTERY = "0000180f-"       # BLE Battery Service
UUID_BATTERY_LEVEL = "00002a19-" # BLE Battery Level characteristic


def is_hid_uuids(uuids: list[str]) -> bool:
    """Does this device's UUID set indicate a HID accessory (knob,
    keyboard, mouse, remote)? Matches BR/EDR HID (0x1124) and BLE
    HOGP (0x1812). Used by the wizards to warn before turning BT off
    while a remote is paired — the VK-01 in particular advertises
    HOGP only, not classic HID."""
    haystack = " ".join(uuids).lower()
    return UUID_HID in haystack or UUID_HOGP in haystack


def _icon_for(class_of_device: int, uuids: list[str], icon_hint: str) -> str:
    """Pick a UI icon slug for a device. Prefers explicit `Icon`
    property from bluez (computer/phone/audio-card/input-keyboard etc.),
    falls back to deriving from UUIDs + class-of-device."""
    if icon_hint:
        return icon_hint
    uu = " ".join(uuids).lower()
    if UUID_HID in uu or UUID_HOGP in uu:
        return "input-keyboard"
    if UUID_A2DP_SINK in uu or UUID_A2DP_SOURCE in uu or UUID_HFP_HF in uu:
        return "audio-headphones"
    # Major device class from CoD (bits 8..12).
    major = (class_of_device >> 8) & 0x1F
    if major == 0x01:  # computer
        return "computer"
    if major == 0x02:  # phone
        return "phone"
    if major == 0x04:  # audio/video
        return "audio-card"
    if major == 0x05:  # peripheral (HID)
        return "input-keyboard"
    return "device"


_MAC_ALIAS_RE = "".join((
    "^",
    "[0-9A-Fa-f]{2}",
    "[-:]",
    "[0-9A-Fa-f]{2}",
    "[-:]",
    "[0-9A-Fa-f]{2}",
    "[-:]",
    "[0-9A-Fa-f]{2}",
    "[-:]",
    "[0-9A-Fa-f]{2}",
    "[-:]",
    "[0-9A-Fa-f]{2}",
    "$",
))


def _real_name(raw_name: str, raw_alias: str) -> str:
    """Return the device's friendly name if there is one, else "".

    Bluez populates `Alias` with a MAC-shaped string (e.g.
    `AA-BB-CC-DD-EE-FF`) when the remote hasn't broadcast a friendly
    name. We don't want to surface that as the "name" — the UI shows
    "Unknown device" + the MAC instead, which is what iPhone does.
    """
    import re

    name = (raw_name or "").strip()
    alias = (raw_alias or "").strip()
    if name and not re.match(_MAC_ALIAS_RE, name):
        return name
    if alias and not re.match(_MAC_ALIAS_RE, alias):
        return alias
    return ""


@dataclass(frozen=True)
class BluetoothDevice:
    """One BT device as seen by the adapter. Built from a bluez
    `org.bluez.Device1` property dict + the device's D-Bus path.

    `battery` reflects the device's BLE Battery Service (0x180f)
    reading via `org.bluez.Battery1.Percentage` — only present for
    devices that advertise the standard battery service (most modern
    keyboards / knobs / headphones do; cheap clones often don't).
    """

    path: str          # /org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF
    address: str       # AA:BB:CC:DD:EE:FF
    name: str          # bluez Name/Alias; "" if the remote didn't
                       # broadcast a friendly name (MAC-shaped aliases
                       # are filtered out — see _real_name)
    icon: str          # one of: computer / phone / audio-card /
                       # audio-headphones / input-keyboard / device
    class_of_device: int
    rssi: int | None   # -dBm, or None when not currently seen
    battery: int | None  # 0..100 percent, or None if device doesn't
                         # advertise org.bluez.Battery1
    battery_capable: bool  # advertises BLE Battery Service UUID
    paired: bool
    connected: bool
    services_resolved: bool
    trusted: bool
    uuids: list[str]   # advertised service UUIDs

    @classmethod
    def from_props(
        cls,
        path: str,
        device_props: dict[str, Any],
        battery_props: dict[str, Any] | None = None,
    ) -> "BluetoothDevice":
        # dbus-next gives us Variant wrappers; the .value attribute
        # is the python-native value.
        def _v(d, k, default=None):
            v = d.get(k)
            return getattr(v, "value", v) if v is not None else default

        uuids_raw = _v(device_props, "UUIDs", []) or []
        uuids = [str(u) for u in uuids_raw]
        battery = None
        if battery_props is not None:
            raw_pct = _v(battery_props, "Percentage")
            if raw_pct is not None:
                try:
                    battery = max(0, min(100, int(raw_pct)))
                except (TypeError, ValueError):
                    battery = None
        return cls(
            path=path,
            address=_v(device_props, "Address", "") or "",
            name=_real_name(
                _v(device_props, "Name", "") or "",
                _v(device_props, "Alias", "") or "",
            ),
            icon=_icon_for(
                _v(device_props, "Class", 0) or 0,
                [str(u) for u in uuids_raw],
                _v(device_props, "Icon", "") or "",
            ),
            class_of_device=int(_v(device_props, "Class", 0) or 0),
            rssi=_v(device_props, "RSSI"),
            battery=battery,
            battery_capable=UUID_BATTERY in " ".join(uuids).lower(),
            paired=bool(_v(device_props, "Paired", False)),
            connected=bool(_v(device_props, "Connected", False)),
            services_resolved=bool(_v(device_props, "ServicesResolved", False)),
            trusted=bool(_v(device_props, "Trusted", False)),
            uuids=uuids,
        )

    def to_json(self) -> dict[str, Any]:
        """Serializable shape for SSE / JSON responses."""
        return {
            "path": self.path,
            "address": self.address,
            "name": self.name,
            "icon": self.icon,
            "class": self.class_of_device,
            "rssi": self.rssi,
            "battery": self.battery,
            "batteryCapable": self.battery_capable,
            "paired": self.paired,
            "connected": self.connected,
            "servicesResolved": self.services_resolved,
            "trusted": self.trusted,
            "uuids": self.uuids,
        }
