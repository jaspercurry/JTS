"""Known third-party HID accessories and their keycode → jasper-control
action mappings.

The bridge daemon (bridge.py) watches /dev/input/event* for any device
whose USB VID/PID matches an entry below, and translates matched key
events into HTTP calls against jasper-control on localhost.

Adding a new HID accessory is a one-entry change in `KNOWN_DEVICES`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


# Subset of evdev keycodes we care about for HID consumer-control
# devices. Full list is in /usr/include/linux/input-event-codes.h
# (also exposed by evdev.ecodes.keys at runtime).
KEY_MUTE = 113
KEY_VOLUMEDOWN = 114
KEY_VOLUMEUP = 115


@dataclass(frozen=True)
class KeyAction:
    """One HID keycode → one HTTP call against jasper-control."""

    method: str   # "POST" or "GET"
    path: str     # e.g. "/volume/adjust"
    body: dict    # JSON body (empty dict = no body)
    # If True, multiple events for this keycode arriving within the
    # bridge's coalesce window are summed into one POST. Use for
    # rotation encoders that emit one event per detent at ~20 Hz —
    # otherwise we hammer jasper-control on a fast spin.
    coalesce: bool = False


@dataclass(frozen=True)
class Device:
    """A supported HID accessory."""

    name: str
    vendor_id: int   # USB VID
    product_id: int  # USB PID
    keymap: Mapping[int, KeyAction]
    # Optional regex (Python `re` syntax) for the BT advertised name.
    # When set, the pair wizard filters discovered devices through
    # this — keeps us from grabbing an unrelated nearby HID device
    # (a stray Apple Magic Mouse / Surface Dial in pair mode would
    # otherwise be the "first match"). Match is `re.search`, so
    # anchors are explicit.
    bt_name_regex: str | None = None


# Anticater VK-01 Desktop Volume Knob (USB-C / BT 5.1 HID).
# Default factory keymap: rotate = vol ±, click = mute. Long-press is
# indistinguishable from a tap on the wire (firmware emits a ~3 ms
# press+release regardless of physical hold duration), so it has no
# entry here — hold-to-talk needs LQ-app reconfig or RE'd Pi-side
# config (deferred).
VK01 = Device(
    name="Anticater VK-01",
    vendor_id=0x514C,
    product_id=0x8850,
    keymap={
        KEY_VOLUMEUP: KeyAction(
            "POST", "/volume/adjust", {"delta_percent": 2}, coalesce=True,
        ),
        KEY_VOLUMEDOWN: KeyAction(
            "POST", "/volume/adjust", {"delta_percent": -2}, coalesce=True,
        ),
        KEY_MUTE: KeyAction("POST", "/volume/mute", {}),
    },
    # Anticater's BT advertised name pattern (per the manual). Case-
    # insensitive — some firmware revs lowercase it.
    bt_name_regex=r"(?i)anticater",
)


KNOWN_DEVICES: list[Device] = [VK01]


def lookup(vendor_id: int, product_id: int) -> Device | None:
    """Return the registry entry for a USB (vid, pid), or None."""
    for d in KNOWN_DEVICES:
        if d.vendor_id == vendor_id and d.product_id == product_id:
            return d
    return None


def lookup_by_name(name: str) -> Device | None:
    """Return the registry entry whose `bt_name_regex` matches `name`.

    Used as a fallback when VID/PID lookup misses — over BT-HID the
    kernel exposes whatever vendor/product the device's HID descriptor
    advertises (the VK-01 reuses Apple's Magic Mouse IDs, for example),
    so USB-VID/PID matching alone misses BT-paired versions of devices
    we already know about. Name matching is the stable identity across
    transports.
    """
    import re

    if not name:
        return None
    for d in KNOWN_DEVICES:
        if d.bt_name_regex and re.search(d.bt_name_regex, name):
            return d
    return None
