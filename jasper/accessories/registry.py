# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Known third-party HID remote profiles and keycode → control mappings.

The bridge daemon (bridge.py) watches /dev/input/event* for any device
matching a profile below, and translates matched key events into HTTP
calls against jasper-control on localhost.

Adding a normal evdev-backed HID remote is a one-entry change in
`KNOWN_PROFILES`. Hardware that needs vendor feature reports, LED
feedback, or a real remote microphone should extend the profile
metadata first, then add the narrow runtime adapter it needs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Mapping, Union

from .constants import WIIM_REMOTE_2_MIC_DEVICE, WIIM_REMOTE_2_NAME_RE


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

CAP_VOLUME = "volume"
CAP_TRANSPORT = "transport"
CAP_MUTE = "mute"
CAP_TAP_GESTURES = "tap-gestures"
CAP_VOICE_HOLD = "voice-hold"

RemoteMicStatus = Literal[
    "none", "not_exposed", "reserved", "linux_audio", "adapter",
]


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
class RemoteIdentity:
    """Stable matchers for one remote profile.

    USB VID/PID is the strict match. Bluetooth HID devices often expose
    different IDs from their USB mode, so profiles may also declare
    advertised-name regexes for the pairing/runtime fallback path.
    """

    usb_ids: tuple[tuple[int, int], ...]
    bt_name_regexes: tuple[str, ...] = ()

    def matches_usb(self, vendor_id: int, product_id: int) -> bool:
        return (vendor_id, product_id) in self.usb_ids

    def matches_name(self, name: str) -> bool:
        import re

        if not name:
            return False
        return any(re.search(pattern, name) for pattern in self.bt_name_regexes)


@dataclass(frozen=True)
class RemoteMicSupport:
    """Remote microphone integration state for this profile.

    The HID bridge does not consume audio. This metadata records whether
    a remote is known to expose a standard Linux capture device, or
    whether a vendor/profile-specific adapter can publish a manual mic
    source for jasper-voice.
    """

    status: RemoteMicStatus = "none"
    detail: str = "No remote microphone integration."
    capture_profile_id: str | None = None
    device: str | None = None
    adapter_service: str | None = None


@dataclass(frozen=True)
class ReservedFeature:
    """Intentional extension point for hardware behavior not wired yet."""

    id: str
    detail: str


@dataclass(frozen=True)
class RemoteProfile:
    """A supported evdev-backed HID remote or knob."""

    id: str
    name: str
    identity: RemoteIdentity
    keymap: Mapping[int, Action]
    capabilities: frozenset[str] = frozenset()
    mic: RemoteMicSupport = field(default_factory=RemoteMicSupport)
    reserved_features: tuple[ReservedFeature, ...] = ()


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
VK01 = RemoteProfile(
    id="anticater_vk01",
    name="Anticater VK-01",
    identity=RemoteIdentity(
        usb_ids=((0x514C, 0x8850),),
        # Anticater's BT advertised name pattern (per the manual).
        # Case-insensitive — some firmware revs lowercase it.
        bt_name_regexes=(r"(?i)anticater",),
    ),
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
    capabilities=frozenset({CAP_VOLUME, CAP_TRANSPORT, CAP_TAP_GESTURES}),
    mic=RemoteMicSupport(
        status="reserved",
        detail=(
            "No standard Linux audio capture device observed for the "
            "tested VK-01. The profile reserves a remote-mic slot for "
            "variants that expose one through ALSA, Bluetooth, or a "
            "future vendor adapter."
        ),
    ),
    reserved_features=(
        ReservedFeature(
            id="true_hold",
            detail=(
                "Factory firmware collapses physical long-press into a "
                "short press+release. If a configured variant emits a "
                "real hold edge, map it here with HoldAction."
            ),
        ),
        ReservedFeature(
            id="remote_mic",
            detail=(
                "If a variant exposes an internal mic, add a capture "
                "profile and route that source into the voice pipeline; "
                "do not fake it through the HID button bridge."
            ),
        ),
    ),
)


# WiiM Voice Remote 2 (BLE HID over GATT).
#
# Captured on jts5.local with Linux evdev on 2026-06-25:
#   - Consumer Control: volume, play/pause, next, previous, mute
#   - Keyboard: input/source as KEY_BACK
#   - Consumer Control: voice as KEY_SEARCH
#
# Input/source needs a JTS source-selection semantic. Voice is mapped
# as hold-to-talk against the WiiM BLE voice-report adapter.
WIIM_REMOTE_2 = RemoteProfile(
    id="wiim_remote_2",
    name="WiiM Remote 2",
    identity=RemoteIdentity(
        usb_ids=((0x2717, 0x32B9),),
        bt_name_regexes=(WIIM_REMOTE_2_NAME_RE,),
    ),
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
            on_press=KeyAction(
                "POST", "/session/start", {"source": "wiim_remote_2"},
            ),
            on_release=KeyAction("POST", "/session/end", {}),
        ),
    },
    capabilities=frozenset({CAP_VOLUME, CAP_TRANSPORT, CAP_MUTE, CAP_VOICE_HOLD}),
    mic=RemoteMicSupport(
        status="adapter",
        capture_profile_id="wiim_remote_2",
        device=WIIM_REMOTE_2_MIC_DEVICE,
        adapter_service="jasper-wiim-remote-mic.service",
        detail=(
            "The remote has a built-in MEMS mic exposed as a HID-over-GATT "
            "voice report, not a standard Linux capture device. "
            "jasper-wiim-remote-mic decodes that stream and forwards it to "
            "the wiim_remote_2 manual mic source."
        ),
    ),
    reserved_features=(
        ReservedFeature(
            id="input_button",
            detail=(
                "KEY_BACK/input-source is captured but not mapped to a "
                "JTS source semantic yet."
            ),
        ),
    ),
)


KNOWN_PROFILES: list[RemoteProfile] = [VK01, WIIM_REMOTE_2]


def lookup(vendor_id: int, product_id: int) -> RemoteProfile | None:
    """Return the profile for a USB (vid, pid), or None."""
    for profile in KNOWN_PROFILES:
        if profile.identity.matches_usb(vendor_id, product_id):
            return profile
    return None


def lookup_by_name(name: str) -> RemoteProfile | None:
    """Return the profile whose Bluetooth name matcher accepts `name`.

    Used as a fallback when VID/PID lookup misses — over BT-HID the
    kernel exposes whatever vendor/product the device's HID descriptor
    advertises (the VK-01 reuses Apple's Magic Mouse IDs, for example),
    so USB-VID/PID matching alone misses BT-paired versions of devices
    we already know about. Name matching is the stable identity across
    transports.
    """
    for profile in KNOWN_PROFILES:
        if profile.identity.matches_name(name):
            return profile
    return None
