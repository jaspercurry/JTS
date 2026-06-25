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
    KEY_MUTE,
    KEY_NEXTSONG,
    KEY_PLAYPAUSE,
    KEY_PREVIOUSSONG,
    KEY_SEARCH,
    KEY_VOLUMEDOWN,
    KEY_VOLUMEUP,
    KNOWN_DEVICES,
    VK01,
    WIIM_REMOTE_2,
    HoldAction,
    KeyAction,
    TapAction,
    lookup,
    lookup_by_name,
)


def test_vk01_in_registry():
    assert VK01 in KNOWN_DEVICES
    assert VK01.vendor_id == 0x514C
    assert VK01.product_id == 0x8850


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


def test_wiim_remote_2_media_keymap_targets_control_routes():
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


@pytest.mark.parametrize("device", KNOWN_DEVICES)
def test_every_registered_device_has_unique_usb_ids(device):
    """Sanity guard for future additions — two devices on the same
    (vid, pid) would cause lookup to silently shadow one."""
    matches = [
        d for d in KNOWN_DEVICES
        if d.vendor_id == device.vendor_id
        and d.product_id == device.product_id
    ]
    assert matches == [device], (
        f"VID/PID collision: {[d.name for d in matches]}"
    )
