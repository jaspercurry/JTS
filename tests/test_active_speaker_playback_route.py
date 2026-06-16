"""ActivePlaybackRouteCapability is a thin reader of the resolved OutputLayout.

The capability adds speaker-group demand accounting + route-fit issues on top of
the route resolution that now lives on ``jasper.output_topology``. These pin that
the route half (device, source, transport width, subwoofer support) is taken
verbatim from the layout, and that the diagnostic vs durable split still maps to
``allow_direct_dac``.
"""

from __future__ import annotations

from jasper.active_speaker.playback_route import (
    DIRECT_DAC_SOURCE,
    MISSING_SOURCE,
    OUTPUTD_ACTIVE_LANE_SOURCE,
    active_playback_route_capability,
    durable_profile_route_capability,
    resolve_active_playback_device,
    resolve_diagnostic_playback_device,
    resolve_durable_profile_playback_device,
)
from jasper.audio_hardware.dac import DUAL_APPLE_USB_C_DAC_4CH, HIFIBERRY_DAC8X
from jasper.output_topology import (
    OUTPUT_TOPOLOGY_KIND,
    OutputTopology,
    resolve_output_layout,
)


def _topology(device_id: str, count: int, *, card_id: str | None = None,
              children: list[dict] | None = None,
              groups: list[dict] | None = None,
              routing: dict | None = None) -> OutputTopology:
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


_TWO_WAY_GROUP = [{
    "id": "mono",
    "label": "Mono",
    "kind": "mono",
    "mode": "active_2_way",
    "channels": [
        {"role": "woofer", "physical_output_index": 0, "identity_verified": True},
        {"role": "tweeter", "physical_output_index": 1, "identity_verified": True,
         "startup_muted": True, "protection_required": True,
         "protection_status": "present"},
    ],
}]


def test_diagnostic_capability_mirrors_resolved_layout() -> None:
    topo = _topology(HIFIBERRY_DAC8X.id, 8, card_id="DAC8", groups=_TWO_WAY_GROUP,
                     routing={"mono_group_id": "mono"})
    layout = resolve_output_layout(topo, allow_direct_dac=True)
    cap = active_playback_route_capability(topo)

    assert cap.playback_device == layout.playback_device
    assert cap.playback_device_source == layout.playback_device_source == DIRECT_DAC_SOURCE
    assert cap.transport_channel_count == layout.transport_channel_count == 8
    assert cap.subwoofer_supported == layout.subwoofer_supported is True
    # Demand half is computed from the speaker groups.
    assert cap.required_active_output_count == 2
    assert cap.active_group_count == 1
    assert cap.fits_required_outputs is True
    assert cap.ready is True


def test_durable_capability_uses_outputd_lane_only() -> None:
    # DAC8x has no outputd lane yet, so the durable capability resolves MISSING
    # (no direct-DAC fallback) — unlike the diagnostic capability.
    topo = _topology(HIFIBERRY_DAC8X.id, 8, card_id="DAC8", groups=_TWO_WAY_GROUP,
                     routing={"mono_group_id": "mono"})
    durable = durable_profile_route_capability(topo)
    layout = resolve_output_layout(topo, allow_direct_dac=False)
    assert durable.playback_device is None
    assert durable.playback_device_source == layout.playback_device_source == MISSING_SOURCE
    assert durable.transport_channel_count == 0
    # An active layout with no resolved route is a blocker.
    assert any(i["code"] == "active_playback_route_unavailable" for i in durable.issues)


def test_dual_apple_capability_reads_outputd_lane_width() -> None:
    children = [
        {"child_id": "apple_dac_1", "device_id": "apple_usb_c_dongle",
         "device_label": "A", "physical_output_indexes": [0, 1], "card_id": "AppleA"},
        {"child_id": "apple_dac_2", "device_id": "apple_usb_c_dongle",
         "device_label": "B", "physical_output_indexes": [2, 3], "card_id": "AppleB"},
    ]
    topo = _topology(DUAL_APPLE_USB_C_DAC_4CH.id, 4, children=children,
                     groups=_TWO_WAY_GROUP, routing={"mono_group_id": "mono"})
    cap = active_playback_route_capability(topo)
    assert cap.playback_device_source == OUTPUTD_ACTIVE_LANE_SOURCE
    assert cap.transport_channel_count == 4
    assert cap.ready is True


def test_compat_resolver_aliases_track_their_modes() -> None:
    topo = _topology(HIFIBERRY_DAC8X.id, 8, card_id="DAC8")
    # The compat name routes to the diagnostic resolver.
    assert resolve_active_playback_device(topo) == resolve_diagnostic_playback_device(topo)
    assert resolve_diagnostic_playback_device(topo) == ("hw:CARD=DAC8,DEV=0", DIRECT_DAC_SOURCE)
    assert resolve_durable_profile_playback_device(topo) == (None, MISSING_SOURCE)
