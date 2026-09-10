# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Stable-identity active-output layout resolution."""

from __future__ import annotations

from jasper.audio_hardware.dac import (
    APPLE_USB_C_DONGLE,
    DUAL_APPLE_USB_C_DAC_4CH,
    HIFIBERRY_DAC8X,
)
from jasper.camilla_config_contract import ACTIVE_OUTPUTD_PLAYBACK_DEVICE
from jasper.fanin_coupling import RING_ACTIVE_PLAYBACK_DEVICE
from jasper.output_topology import (
    ACTIVE_PLAYBACK_DEVICE_ENV,
    EXPLICIT_SOURCE,
    MISSING_SOURCE,
    OUTPUT_TOPOLOGY_KIND,
    OUTPUTD_ACTIVE_LANE_SOURCE,
    OutputLayout,
    OutputTopology,
    resolve_output_layout,
)


GENERIC_SINGLE_DAC = "generic_single_dac"


def _topology(
    device_id: str,
    count: int,
    *,
    card_id: str | None = None,
    children: list[dict] | None = None,
) -> OutputTopology:
    hardware: dict = {
        "device_id": device_id,
        "device_label": "Test device",
        "physical_output_count": count,
    }
    if card_id:
        hardware["card_id"] = card_id
    if children:
        hardware["child_devices"] = children
    return OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "t",
        "name": "n",
        "status": "draft",
        "hardware": hardware,
        "speaker_groups": [],
        "routing": {},
    })


def test_dac8x_resolves_to_the_active_ring() -> None:
    """#2285 P2 renamed this from ``..._resolves_to_outputd_active_lane``.

    The chooser answers the ACTIVE RING now, unconditionally — the snd-aloop
    ACTIVE lane stopped being a legal outputd endpoint. The SOURCE token is
    deliberately unchanged (it names the LANE ROLE, not the transport), which is
    why that half of the assertion still reads the same and nothing keyed on it
    needed an edit.
    """
    layout = resolve_output_layout(_topology(HIFIBERRY_DAC8X.id, 8, card_id="DAC8"))

    assert layout.playback_device == RING_ACTIVE_PLAYBACK_DEVICE
    assert layout.playback_device != ACTIVE_OUTPUTD_PLAYBACK_DEVICE
    assert layout.playback_device_source == OUTPUTD_ACTIVE_LANE_SOURCE
    assert layout.transport_channel_count == 8


def test_apple_usb_c_dongle_resolves_to_the_width_two_active_ring() -> None:
    layout = resolve_output_layout(
        _topology(APPLE_USB_C_DONGLE.id, 2, card_id="Apple")
    )

    assert layout.playback_device == RING_ACTIVE_PLAYBACK_DEVICE
    assert layout.playback_device_source == OUTPUTD_ACTIVE_LANE_SOURCE
    assert layout.transport_channel_count == 2


def test_no_active_lane_single_dac_is_missing_without_direct_fallback() -> None:
    layout = resolve_output_layout(_topology(GENERIC_SINGLE_DAC, 8, card_id="DAC8"))

    assert layout.playback_device is None
    assert layout.playback_device_source == MISSING_SOURCE
    assert layout.transport_channel_count == 0
    assert layout.subwoofer_supported is False


def test_dual_apple_uses_the_active_ring() -> None:
    children = [
        {
            "child_id": "apple_dac_1",
            "device_id": "apple_usb_c_dongle",
            "device_label": "A",
            "physical_output_indexes": [0, 1],
            "card_id": "AppleA",
        },
        {
            "child_id": "apple_dac_2",
            "device_id": "apple_usb_c_dongle",
            "device_label": "B",
            "physical_output_indexes": [2, 3],
            "card_id": "AppleB",
        },
    ]
    layout = resolve_output_layout(
        _topology(DUAL_APPLE_USB_C_DAC_4CH.id, 4, children=children),
    )
    assert layout.playback_device == RING_ACTIVE_PLAYBACK_DEVICE
    assert layout.playback_device_source == OUTPUTD_ACTIVE_LANE_SOURCE
    assert layout.transport_channel_count == 4


def test_explicit_override_wins_over_profile() -> None:
    layout = resolve_output_layout(
        _topology(HIFIBERRY_DAC8X.id, 8, card_id="DAC8"),
        env={ACTIVE_PLAYBACK_DEVICE_ENV: "hw:Active"},
    )
    assert layout.playback_device == "hw:Active"
    assert layout.playback_device_source == EXPLICIT_SOURCE
    assert layout.transport_channel_count == 8

    by_arg = resolve_output_layout(
        _topology(HIFIBERRY_DAC8X.id, 8, card_id="DAC8"),
        playback_device="hw:FromArg",
        env={ACTIVE_PLAYBACK_DEVICE_ENV: "hw:FromEnv"},
    )
    assert by_arg.playback_device == "hw:FromArg"


def test_isinstance_layout_type() -> None:
    layout = resolve_output_layout(_topology(HIFIBERRY_DAC8X.id, 8, card_id="DAC8"))
    assert isinstance(layout, OutputLayout)
