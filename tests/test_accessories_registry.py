# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Coverage for the accessory registry — small, declarative, but the
bridge daemon's whole behaviour pivots on `lookup()` returning the
right entry, so worth one test pass.
"""
from __future__ import annotations

import pytest

from jasper.accessories.registry import (
    CAP_MUTE,
    CAP_TAP_GESTURES,
    CAP_TRANSPORT,
    CAP_VOICE_HOLD,
    CAP_VOLUME,
    KEY_MUTE,
    KEY_NEXTSONG,
    KEY_PLAYPAUSE,
    KEY_PREVIOUSSONG,
    KEY_SEARCH,
    KEY_VOLUMEDOWN,
    KEY_VOLUMEUP,
    KNOWN_DEVICES,
    KNOWN_PROFILES,
    VK01,
    WIIM_REMOTE_2,
    HoldAction,
    KeyAction,
    RemoteProfile,
    TapAction,
    lookup,
    lookup_by_name,
)


def test_vk01_in_registry():
    assert KNOWN_DEVICES is KNOWN_PROFILES
    assert VK01 in KNOWN_PROFILES
    assert isinstance(VK01, RemoteProfile)
    assert VK01.id == "anticater_vk01"
    assert VK01.vendor_id == 0x514C
    assert VK01.product_id == 0x8850
    assert VK01.identity.usb_ids == ((0x514C, 0x8850),)
    assert VK01.bt_name_regex == r"(?i)anticater"
    assert VK01.capabilities == frozenset({
        CAP_VOLUME, CAP_TRANSPORT, CAP_TAP_GESTURES,
    })


def test_lookup_finds_vk01_by_usb_ids():
    entry = lookup(0x514C, 0x8850)
    assert entry is VK01


def test_lookup_finds_wiim_remote_2_by_bluetooth_hid_ids():
    entry = lookup(0x2717, 0x32B9)
    assert entry is WIIM_REMOTE_2


def test_lookup_returns_none_for_unknown():
    assert lookup(0xDEAD, 0xBEEF) is None
    assert lookup(0x0000, 0x0000) is None


def test_vk01_default_keymap_covers_rotate_and_click():
    keymap = VK01.keymap
    assert KEY_VOLUMEUP in keymap
    assert KEY_VOLUMEDOWN in keymap
    assert KEY_MUTE in keymap


def test_vk01_rotate_actions_coalesce_and_target_volume_adjust():
    up = VK01.keymap[KEY_VOLUMEUP]
    down = VK01.keymap[KEY_VOLUMEDOWN]
    for action in (up, down):
        assert action.method == "POST"
        assert action.path == "/volume/adjust"
        assert action.coalesce is True
        assert "delta_percent" in action.body
    assert up.body["delta_percent"] > 0
    assert down.body["delta_percent"] < 0
    # CW and CCW should be symmetric.
    assert up.body["delta_percent"] == -down.body["delta_percent"]


def test_vk01_click_is_tap_action_for_transport():
    """VK-01's click (KEY_MUTE keycode) maps to a TapAction so single
    tap toggles play/pause, double skips, triple goes back — same
    semantics as the WiFi dial. The keycode happens to be KEY_MUTE
    because that's what the VK-01's HID descriptor sends; we treat it
    as an opaque button-id and dispatch by tap count."""
    click = VK01.keymap[KEY_MUTE]
    assert isinstance(click, TapAction), (
        "VK-01 click should be TapAction, got %r" % type(click)
    )
    assert click.on_single == KeyAction("POST", "/transport/toggle", {})
    assert click.on_double == KeyAction("POST", "/transport/next", {})
    assert click.on_triple == KeyAction("POST", "/transport/previous", {})
    # Sanity: window must be positive — a 0 ms window degenerates
    # to "every tap fires immediately" which defeats the gesture.
    assert click.window_ms > 0


def test_vk01_profile_reserves_hold_and_mic_extension_points():
    assert VK01.mic.status == "reserved"
    assert VK01.mic.capture_profile_id is None
    assert "standard Linux audio capture device" in VK01.mic.detail
    reserved = {feature.id: feature.detail for feature in VK01.reserved_features}
    assert "true_hold" in reserved
    assert "remote_mic" in reserved
    assert "HoldAction" in reserved["true_hold"]
    assert "voice pipeline" in reserved["remote_mic"]


def test_wiim_remote_2_media_keymap_targets_control_routes():
    assert WIIM_REMOTE_2.id == "wiim_remote_2"
    assert WIIM_REMOTE_2.capabilities == frozenset({
        CAP_VOLUME, CAP_TRANSPORT, CAP_MUTE, CAP_VOICE_HOLD,
    })
    assert WIIM_REMOTE_2.mic.status == "reserved"
    assert "MEMS mic" in WIIM_REMOTE_2.mic.detail
    keymap = WIIM_REMOTE_2.keymap
    assert keymap[KEY_VOLUMEUP] == KeyAction(
        "POST", "/volume/adjust", {"delta_percent": 2}, coalesce=True,
    )
    assert keymap[KEY_VOLUMEDOWN] == KeyAction(
        "POST", "/volume/adjust", {"delta_percent": -2}, coalesce=True,
    )
    assert keymap[KEY_PLAYPAUSE] == KeyAction(
        "POST", "/transport/toggle", {},
    )
    assert keymap[KEY_NEXTSONG] == KeyAction("POST", "/transport/next", {})
    assert keymap[KEY_PREVIOUSSONG] == KeyAction(
        "POST", "/transport/previous", {},
    )
    assert keymap[KEY_MUTE] == KeyAction("POST", "/volume/mute", {})
    assert keymap[KEY_SEARCH] == HoldAction(
        on_press=KeyAction("POST", "/session/start", {}),
        on_release=KeyAction("POST", "/session/end", {}),
    )


def test_wiim_remote_2_name_fallback_matches_bluez_name():
    assert lookup_by_name("WiiM Remote 2") is WIIM_REMOTE_2
    assert lookup_by_name("wiim remote 2 consumer control") is WIIM_REMOTE_2


def test_every_registered_profile_has_unique_usb_ids():
    """Sanity guard for future additions — two profiles sharing any
    (vid, pid), including alternate transport IDs, would make lookup()
    silently shadow one."""
    seen: dict[tuple[int, int], RemoteProfile] = {}
    for profile in KNOWN_PROFILES:
        assert profile.identity.usb_ids, f"{profile.id} must declare a USB ID"
        for usb_id in profile.identity.usb_ids:
            assert usb_id not in seen, (
                f"VID/PID collision {usb_id!r}: "
                f"{seen[usb_id].name} and {profile.name}"
            )
            seen[usb_id] = profile


@pytest.mark.parametrize("profile", KNOWN_PROFILES)
def test_every_registered_profile_has_known_mic_status(profile):
    assert profile.mic.status in {
        "none", "not_exposed", "reserved", "linux_audio",
    }
