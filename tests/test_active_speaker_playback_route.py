# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""ActivePlaybackRouteCapability is a thin reader of the resolved OutputLayout."""

from __future__ import annotations

from jasper.active_speaker.playback_route import (
    MISSING_SOURCE,
    OUTPUTD_ACTIVE_LANE_SOURCE,
    active_lane_capability_gap,
    active_playback_route_capability,
    resolve_active_playback_device,
)
from jasper.audio_hardware.dac import (
    APPLE_USB_C_DONGLE,
    DUAL_APPLE_USB_C_DAC_4CH,
    HIFIBERRY_DAC8X,
    INNOMAKER_HIFI_AMP_PRO,
)
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


def test_apple_dongle_capability_reads_width_two_outputd_active_lane() -> None:
    topo = _topology(
        APPLE_USB_C_DONGLE.id,
        2,
        card_id="Apple",
        groups=_TWO_WAY_GROUP,
        routing={"mono_group_id": "mono"},
    )
    cap = active_playback_route_capability(topo)

    assert cap.playback_device_source == OUTPUTD_ACTIVE_LANE_SOURCE
    assert cap.transport_channel_count == 2
    assert cap.required_active_output_count == 2
    assert cap.fits_required_outputs is True
    assert cap.ready is True
    assert cap.issues == ()


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


# ---- active-lane capability gap -------------------------------------------
#
# The permanently-undrivable pairing: a roleful layout on a DAC that declares
# no active outputd lane. CamillaDSP then plays the roleful graph into the
# active loopback lane while outputd captures the passive one, and the speaker
# emits digital silence. One predicate, read by /state, doctor, and the
# /sound/setup/ save guard.


def test_roleful_layout_on_a_dac_without_an_active_lane_is_a_gap() -> None:
    topo = _topology(
        INNOMAKER_HIFI_AMP_PRO.id,
        2,
        groups=_TWO_WAY_GROUP,
        routing={"mono_group_id": "mono"},
    )

    gap = active_lane_capability_gap(topo)

    assert gap is not None
    assert gap.device_id == INNOMAKER_HIFI_AMP_PRO.id
    # The registry owns the name, not the topology's saved "Test device" label.
    assert gap.device_label == INNOMAKER_HIFI_AMP_PRO.label


def test_passive_layout_on_the_same_dac_is_not_a_gap() -> None:
    topo = _topology(INNOMAKER_HIFI_AMP_PRO.id, 2, groups=[{
        "id": "left",
        "label": "Left",
        "kind": "left",
        "mode": "full_range_passive",
        "channels": [
            {
                "role": "full_range",
                "physical_output_index": 0,
                "identity_verified": True,
            },
        ],
    }])

    assert active_lane_capability_gap(topo) is None


def test_active_capable_dac_is_not_a_gap() -> None:
    topo = _topology(
        HIFIBERRY_DAC8X.id,
        8,
        card_id="DAC8",
        groups=_TWO_WAY_GROUP,
        routing={"mono_group_id": "mono"},
    )

    assert active_lane_capability_gap(topo) is None


def test_unrecognized_dac_is_not_reported_as_a_gap() -> None:
    """Strict on purpose: with no profile there is no capability to read, so
    the predicate stays silent rather than blocking a save on hardware the
    registry has simply not met."""
    topo = _topology(
        GENERIC_SINGLE_DAC,
        8,
        card_id="DAC8",
        groups=_TWO_WAY_GROUP,
        routing={"mono_group_id": "mono"},
    )

    assert active_lane_capability_gap(topo) is None
