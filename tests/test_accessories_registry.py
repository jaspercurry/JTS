"""Coverage for the accessory registry — small, declarative, but the
bridge daemon's whole behaviour pivots on `lookup()` returning the
right entry, so worth one test pass.
"""
from __future__ import annotations

import pytest

from jasper.accessories.registry import (
    KEY_MUTE,
    KEY_VOLUMEDOWN,
    KEY_VOLUMEUP,
    KNOWN_DEVICES,
    VK01,
    lookup,
)


def test_vk01_in_registry():
    assert VK01 in KNOWN_DEVICES
    assert VK01.vendor_id == 0x514C
    assert VK01.product_id == 0x8850


def test_lookup_finds_vk01_by_usb_ids():
    entry = lookup(0x514C, 0x8850)
    assert entry is VK01


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


def test_vk01_click_targets_volume_mute_no_coalesce():
    mute = VK01.keymap[KEY_MUTE]
    assert mute.method == "POST"
    assert mute.path == "/volume/mute"
    assert mute.coalesce is False
    assert mute.body == {}


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
