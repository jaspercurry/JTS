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
UUID_HID = "00001124-"           # Human Interface Device


def _icon_for(class_of_device: int, uuids: list[str], icon_hint: str) -> str:
    """Pick a UI icon slug for a device. Prefers explicit `Icon`
    property from bluez (computer/phone/audio-card/input-keyboard etc.),
    falls back to deriving from UUIDs + class-of-device."""
    if icon_hint:
        return icon_hint
    uu = " ".join(uuids).lower()
    if UUID_HID in uu:
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


@dataclass(frozen=True)
class BluetoothDevice:
    """One BT device as seen by the adapter. Built from a bluez
    `org.bluez.Device1` property dict + the device's D-Bus path."""

    path: str          # /org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF
    address: str       # AA:BB:CC:DD:EE:FF
    name: str          # bluez Name or Alias, "" if unset
    icon: str          # one of: computer / phone / audio-card /
                       # audio-headphones / input-keyboard / device
    class_of_device: int
    rssi: int | None   # -dBm, or None when not currently seen
    paired: bool
    connected: bool
    trusted: bool
    uuids: list[str]   # advertised service UUIDs

    @classmethod
    def from_props(cls, path: str, props: dict[str, Any]) -> "BluetoothDevice":
        # dbus-next gives us Variant wrappers; the .value attribute
        # is the python-native value.
        def _v(k, default=None):
            v = props.get(k)
            return getattr(v, "value", v) if v is not None else default

        uuids_raw = _v("UUIDs", []) or []
        return cls(
            path=path,
            address=_v("Address", "") or "",
            name=(_v("Name") or _v("Alias") or "") or "",
            icon=_icon_for(
                _v("Class", 0) or 0,
                [str(u) for u in uuids_raw],
                _v("Icon", "") or "",
            ),
            class_of_device=int(_v("Class", 0) or 0),
            rssi=_v("RSSI"),  # None if not advertised this scan
            paired=bool(_v("Paired", False)),
            connected=bool(_v("Connected", False)),
            trusted=bool(_v("Trusted", False)),
            uuids=[str(u) for u in uuids_raw],
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
            "paired": self.paired,
            "connected": self.connected,
            "trusted": self.trusted,
            "uuids": self.uuids,
        }
