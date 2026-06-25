# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Known third-party HID accessories and their keycode → jasper-control
action mappings.

The bridge daemon (bridge.py) watches /dev/input/event* for any device
whose USB VID/PID matches an entry below, and translates matched key
events into HTTP calls against jasper-control on localhost.

Adding a new HID accessory is a one-entry change in `KNOWN_DEVICES`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Union


# Subset of evdev keycodes we care about for HID consumer-control
# devices. Full list is in /usr/include/linux/input-event-codes.h
# (also exposed by evdev.ecodes.keys at runtime).
KEY_MUTE = 113
KEY_VOLUMEDOWN = 114
KEY_VOLUMEUP = 115
KEY_PREVIOUSSONG = 165
KEY_NEXTSONG = 163
KEY_PLAYPAUSE = 164
KEY_SEARCH = 217


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
class TapAction:
    """One HID keycode → tap-count-dependent HTTP calls.

    Each press of the bound key advances a counter; the counter
    commits after `window_ms` of quiescence, or immediately on the
    third tap (which is unambiguous — we don't define quadruple-tap
    semantics, so no need to wait further).

    Cost: each single tap is delayed by `window_ms` before its HTTP
    call fires (we have to wait to be sure no second tap is coming).
    For mute-style "I need this now" actions, use KeyAction instead.
    """

    on_single: KeyAction
    on_double: KeyAction | None = None
    on_triple: KeyAction | None = None
    # Inter-tap quiescence window. 400 ms is a deliberate compromise:
    # tight enough that single-tap doesn't feel laggy, loose enough
    # that natural human double/triple-taps register reliably. macOS's
    # default double-click speed is ~500 ms; we run a touch tighter.
    # Earlier value (280 ms) was too aggressive for BT HID — physical
    # knob clicks add springback delay between presses; user couldn't
    # land three taps within the window (verified on VK-01 hardware
    # 2026-05-23).
    window_ms: int = 400


@dataclass(frozen=True)
class HoldAction:
    """One HID keycode press/release pair → two HTTP calls.

    Use for deliberate "hold while active" controls such as the WiiM
    voice button: press starts the manual session, release ends it.
    """

    on_press: KeyAction
    on_release: KeyAction


Action = Union[KeyAction, TapAction, HoldAction]


@dataclass(frozen=True)
class Device:
    """A supported HID accessory."""

    name: str
    vendor_id: int   # USB VID
    product_id: int  # USB PID
    keymap: Mapping[int, Action]
    # Optional regex (Python `re` syntax) for the BT advertised name.
    # When set, the pair wizard filters discovered devices through
    # this — keeps us from grabbing an unrelated nearby HID device
    # (a stray Apple Magic Mouse / Surface Dial in pair mode would
    # otherwise be the "first match"). Match is `re.search`, so
    # anchors are explicit.
    bt_name_regex: str | None = None


# Anticater VK-01 Desktop Volume Knob (USB-C / BT 5.1 HID).
# Default factory keymap: rotate = vol ±, click sends KEY_MUTE. Long-
# press is indistinguishable from a tap on the wire (firmware emits a
# ~3 ms press+release regardless of physical hold duration), so it has
# no entry here — hold-to-talk needs LQ-app reconfig or RE'd Pi-side
# config (deferred).
#
# We map the click (KEY_MUTE keycode) to a TapAction so single = play/
# pause toggle, double = next, triple = previous — matching the dial's
# semantics. The hardware sends KEY_MUTE per press but we treat the
# keycode as opaque button-id and dispatch by tap count.
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
        KEY_MUTE: TapAction(
            on_single=KeyAction("POST", "/transport/toggle", {}),
            on_double=KeyAction("POST", "/transport/next", {}),
            on_triple=KeyAction("POST", "/transport/previous", {}),
        ),
    },
    # Anticater's BT advertised name pattern (per the manual). Case-
    # insensitive — some firmware revs lowercase it.
    bt_name_regex=r"(?i)anticater",
)


# WiiM Voice Remote 2 (BLE HID over GATT).
#
# Captured on jts5.local with Linux evdev on 2026-06-25:
#   - Consumer Control: volume, play/pause, next, previous, mute
#   - Keyboard: input/source as KEY_BACK
#   - Consumer Control: voice as KEY_SEARCH
#
# Input/source needs a JTS source-selection semantic. Voice is mapped
# as hold-to-talk against the normal JTS mic pipeline; the WiiM Remote
# 2's MEMS mic does not expose as a standard Linux audio capture device.
WIIM_REMOTE_2 = Device(
    name="WiiM Remote 2",
    vendor_id=0x2717,
    product_id=0x32B9,
    keymap={
        KEY_VOLUMEUP: KeyAction(
            "POST", "/volume/adjust", {"delta_percent": 2}, coalesce=True,
        ),
        KEY_VOLUMEDOWN: KeyAction(
            "POST", "/volume/adjust", {"delta_percent": -2}, coalesce=True,
        ),
        KEY_PLAYPAUSE: KeyAction("POST", "/transport/toggle", {}),
        KEY_NEXTSONG: KeyAction("POST", "/transport/next", {}),
        KEY_PREVIOUSSONG: KeyAction("POST", "/transport/previous", {}),
        KEY_MUTE: KeyAction("POST", "/volume/mute", {}),
        KEY_SEARCH: HoldAction(
            on_press=KeyAction("POST", "/session/start", {}),
            on_release=KeyAction("POST", "/session/end", {}),
        ),
    },
    bt_name_regex=r"(?i)\bwiim remote 2\b",
)


KNOWN_DEVICES: list[Device] = [VK01, WIIM_REMOTE_2]


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
