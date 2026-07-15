# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Coverage for the accessory registry — small, declarative, but the
bridge daemon's whole behaviour pivots on `lookup()` returning the
right entry, so worth one test pass.
"""
from __future__ import annotations

import pytest

from jasper.accessories.constants import WIIM_REMOTE_2_MIC_DEVICE, WIIM_REMOTE_2_NAME_RE
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
from jasper.audio_io import parse_udp_device
from jasper.usb_mic import USB_HOST_MIC_UDP_PORT


def test_vk01_in_registry():
    assert VK01 in KNOWN_PROFILES
    assert isinstance(VK01, RemoteProfile)
    assert VK01.id == "anticater_vk01"
    assert VK01.identity.usb_ids == ((0x514C, 0x8850),)
    assert VK01.identity.bt_name_regexes == (r"(?i)anticater",)
    assert lookup_by_name("ANTICATER_MINI") is VK01
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
    assert WIIM_REMOTE_2.mic.status == "adapter"
    assert WIIM_REMOTE_2.mic.capture_profile_id == "wiim_remote_2"
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
        on_press=KeyAction(
            "POST", "/session/start", {"source": "wiim_remote_2"},
        ),
        on_release=KeyAction("POST", "/session/end", {}),
    )


def test_wiim_remote_2_name_fallback_matches_bluez_name():
    assert lookup_by_name("WiiM Remote 2") is WIIM_REMOTE_2
    assert lookup_by_name("wiim remote 2 consumer control") is WIIM_REMOTE_2


def test_wiim_remote_2_declares_adapter_mic_source():
    assert WIIM_REMOTE_2.mic.status == "adapter"
    assert WIIM_REMOTE_2.mic.capture_profile_id == "wiim_remote_2"
    assert WIIM_REMOTE_2.mic.device == WIIM_REMOTE_2_MIC_DEVICE
    assert WIIM_REMOTE_2.mic.adapter_service == "jasper-wiim-remote-mic.service"


def test_wiim_remote_2_name_re_ssot_registry_matches_adapter():
    """Registry bt_name_regexes and adapter WIIM_REMOTE_2_NAME_RE are the same object.

    Both callers import from constants.py; this test pins the contract so a
    drift (someone redefining the pattern in one file) makes CI red immediately.
    Drift failure mode: reconciler activates the adapter via the registry match,
    but the adapter's stale regex fails to find the voice characteristic →
    silent wiim_remote_mic.not_ready retry loop with no audio.
    """
    from jasper.accessories.wiim_remote_mic import WIIM_REMOTE_2_NAME_RE as adapter_re

    # Both modules must import the constant (same object identity).
    assert adapter_re is WIIM_REMOTE_2_NAME_RE, (
        "wiim_remote_mic.WIIM_REMOTE_2_NAME_RE is not the constants.py object — "
        "it has been redefined locally and the SSOT has drifted"
    )

    # Registry entry must also use the constants.py value.
    registry_regexes = WIIM_REMOTE_2.identity.bt_name_regexes
    assert len(registry_regexes) == 1
    assert registry_regexes[0] is WIIM_REMOTE_2_NAME_RE, (
        "WIIM_REMOTE_2.identity.bt_name_regexes[0] is not constants.WIIM_REMOTE_2_NAME_RE "
        "— registry has drifted from the SSOT"
    )


def test_adapter_mic_sources_do_not_reuse_reserved_voice_udp_ports():
    """Adapter mics must not collide with AEC/wake/output-reference UDP ports."""
    reserved_ports = {
        9876, 9877, 9878, 9880, 9887, 9888, 9891, USB_HOST_MIC_UDP_PORT,
    }
    for profile in KNOWN_PROFILES:
        if profile.mic.status != "adapter":
            continue
        parsed = parse_udp_device(profile.mic.device or "")
        assert parsed is not None, f"{profile.id} adapter mic must be UDP-backed"
        assert parsed[1] not in reserved_ports, (
            f"{profile.id} mic source {profile.mic.device} reuses a reserved "
            "voice/wake/reference UDP port"
        )


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
        "none", "not_exposed", "reserved", "linux_audio", "adapter",
    }
    if profile.mic.status == "adapter":
        assert profile.mic.capture_profile_id
        assert profile.mic.device
        assert profile.mic.adapter_service
