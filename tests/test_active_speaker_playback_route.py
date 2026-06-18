"""ActivePlaybackRouteCapability is a thin reader of the resolved OutputLayout."""

from __future__ import annotations

from jasper.active_speaker.playback_route import (
    MISSING_SOURCE,
    OUTPUTD_ACTIVE_LANE_SOURCE,
    active_playback_route_capability,
    resolve_active_playback_device,
)
from jasper.audio_hardware.dac import DUAL_APPLE_USB_C_DAC_4CH, HIFIBERRY_DAC8X
from jasper.output_topology import (
    EXPLICIT_SOURCE,
    OUTPUT_TOPOLOGY_KIND,
    OutputTopology,
    resolve_output_layout,
)


def _topology(
    device_id: str,
    count: int,
    *,
    card_id: str | None = None,
    children: list[dict] | None = None,
    groups: list[dict] | None = None,
    routing: dict | None = None,
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
        "speaker_groups": groups or [],
        "routing": routing or {},
    })


GENERIC_SINGLE_DAC = "generic_single_dac"


_TWO_WAY_GROUP = [{
    "id": "mono",
    "label": "Mono",
    "kind": "mono",
    "mode": "active_2_way",
    "channels": [
        {"role": "woofer", "physical_output_index": 0, "identity_verified": True},
        {
            "role": "tweeter",
            "physical_output_index": 1,
            "identity_verified": True,
            "startup_muted": True,
            "protection_required": True,
            "protection_status": "present",
        },
    ],
}]


def test_capability_mirrors_missing_layout_without_direct_dac_fallback() -> None:
    topo = _topology(
        GENERIC_SINGLE_DAC,
        8,
        card_id="DAC8",
        groups=_TWO_WAY_GROUP,
        routing={"mono_group_id": "mono"},
    )
    layout = resolve_output_layout(topo)
    cap = active_playback_route_capability(topo)

    assert layout.playback_device is None
    assert cap.playback_device is None
    assert cap.playback_device_source == layout.playback_device_source == MISSING_SOURCE
    assert cap.transport_channel_count == 0
    assert cap.required_active_output_count == 2
    assert cap.active_group_count == 1
    assert cap.fits_required_outputs is False
    assert cap.ready is False
    assert any(i["code"] == "active_playback_route_unavailable" for i in cap.issues)


def test_dac8x_capability_reads_active_outputd_lane() -> None:
    topo = _topology(
        HIFIBERRY_DAC8X.id,
        8,
        card_id="DAC8",
        groups=_TWO_WAY_GROUP,
        routing={"mono_group_id": "mono"},
    )
    cap = active_playback_route_capability(topo)

    assert cap.playback_device_source == OUTPUTD_ACTIVE_LANE_SOURCE
    assert cap.transport_channel_count == 8
    assert cap.required_active_output_count == 2
    assert cap.fits_required_outputs is True
    assert cap.ready is True


def test_dual_apple_capability_reads_outputd_lane_width() -> None:
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
    topo = _topology(
        DUAL_APPLE_USB_C_DAC_4CH.id,
        4,
        children=children,
        groups=_TWO_WAY_GROUP,
        routing={"mono_group_id": "mono"},
    )
    cap = active_playback_route_capability(topo)

    assert cap.playback_device_source == OUTPUTD_ACTIVE_LANE_SOURCE
    assert cap.transport_channel_count == 4
    assert cap.ready is True


def test_explicit_lab_pcm_is_the_only_non_outputd_route() -> None:
    topo = _topology(GENERIC_SINGLE_DAC, 8, card_id="DAC8")

    assert resolve_active_playback_device(topo) == (None, MISSING_SOURCE)
    assert resolve_active_playback_device(topo, playback_device="hw:Lab") == (
        "hw:Lab",
        EXPLICIT_SOURCE,
    )
