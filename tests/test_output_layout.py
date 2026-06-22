# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Stable-identity active-output layout + DAC-agnostic transport plan."""

from __future__ import annotations

import pytest

from jasper.audio_hardware.dac import (
    APPLE_USB_C_DONGLE,
    DUAL_APPLE_USB_C_DAC_4CH,
    HIFIBERRY_DAC8X,
    ChannelMapEntry,
    DacProfile,
)
from jasper.camilla_config_contract import ACTIVE_OUTPUTD_PLAYBACK_DEVICE
from jasper.output_topology import (
    ACTIVE_PLAYBACK_DEVICE_ENV,
    EXPLICIT_SOURCE,
    MISSING_SOURCE,
    OUTPUT_LAYOUT_KIND,
    OUTPUT_TOPOLOGY_KIND,
    OUTPUT_TRANSPORT_PLAN_KIND,
    OUTPUTD_ACTIVE_LANE_SOURCE,
    TRANSPORT_SINK_COMPOSITE,
    TRANSPORT_SINK_SINGLE_ALSA,
    OutputChildDevice,
    OutputHardware,
    OutputLayout,
    OutputTopology,
    OutputTopologyError,
    OutputTransportPlan,
    _build_outputd_transport_plan,
    _transport_sink_for_kind,
    is_stable_card_pcm,
    resolve_output_layout,
    stable_card_pcm,
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


def _active_single_profile(**overrides: object) -> DacProfile:
    base: dict[str, object] = dict(
        id="test_active_single",
        label="Test active single",
        kind="single",
        physical_output_count=4,
        coherent_clock_domain=True,
        clock_domain_label="Test clock",
        clock_domain_contract="single_device",
        outputd_sink="alsa",
        supported_card_matches=("test",),
        supports_active_outputd_lane=True,
        active_outputd_lane_channels=4,
    )
    base.update(overrides)
    return DacProfile(**base)  # type: ignore[arg-type]


def test_stable_card_pcm_builds_name_keyed_identity() -> None:
    assert stable_card_pcm("DAC8") == "hw:CARD=DAC8,DEV=0"
    assert stable_card_pcm("  Array  ") == "hw:CARD=Array,DEV=0"
    assert stable_card_pcm("") is None
    assert stable_card_pcm(None) is None


def test_is_stable_card_pcm_rejects_drift_prone_forms() -> None:
    assert is_stable_card_pcm("hw:CARD=DAC8,DEV=0") is True
    assert is_stable_card_pcm("hw:0,0") is False
    assert is_stable_card_pcm("plughw:CARD=DAC8,DEV=0") is False
    assert is_stable_card_pcm("hw:CARD=DAC8") is False
    assert is_stable_card_pcm("hw:CARD=DAC8,DEV=0\n") is False
    assert is_stable_card_pcm("\nhw:CARD=DAC8,DEV=0") is False


def test_transport_plan_rejects_non_stable_dac_pcms() -> None:
    cmap = (ChannelMapEntry(0, 0), ChannelMapEntry(1, 1))
    with pytest.raises(OutputTopologyError, match="stable hw:CARD="):
        OutputTransportPlan(
            TRANSPORT_SINK_SINGLE_ALSA,
            2,
            cmap,
            ("hw:0,0",),
            "single_device",
        )


def test_transport_plan_validates_shape_and_channel_map() -> None:
    cmap = (ChannelMapEntry(0, 0), ChannelMapEntry(1, 1))
    with pytest.raises(OutputTopologyError, match="unsupported transport sink"):
        OutputTransportPlan("single_alsa_multi", 2, cmap, (), "single_device")
    with pytest.raises(OutputTopologyError, match="one entry per transport channel"):
        OutputTransportPlan(TRANSPORT_SINK_SINGLE_ALSA, 4, cmap, (), "single_device")
    with pytest.raises(OutputTopologyError, match="same physical_dac_channel"):
        OutputTransportPlan(
            TRANSPORT_SINK_SINGLE_ALSA,
            2,
            (ChannelMapEntry(0, 0), ChannelMapEntry(1, 0)),
            (),
            "single_device",
        )


def test_transport_plan_to_dict_shape() -> None:
    plan = OutputTransportPlan(
        sink=TRANSPORT_SINK_SINGLE_ALSA,
        transport_channels=2,
        channel_map=(ChannelMapEntry(0, 1), ChannelMapEntry(1, 0)),
        dac_pcms=("hw:CARD=DAC8,DEV=0",),
        clock_domain_contract="single_device",
    )
    out = plan.to_dict()
    assert out["kind"] == OUTPUT_TRANSPORT_PLAN_KIND
    assert out["sink"] == "single_alsa"
    assert out["transport_channels"] == 2
    assert out["channel_map"] == [
        {"camilla_out_index": 0, "physical_dac_channel": 1},
        {"camilla_out_index": 1, "physical_dac_channel": 0},
    ]
    assert out["dac_pcms"] == ["hw:CARD=DAC8,DEV=0"]


def test_transport_sink_maps_clock_domain_shape() -> None:
    assert _transport_sink_for_kind("single") == TRANSPORT_SINK_SINGLE_ALSA
    assert _transport_sink_for_kind("composite") == TRANSPORT_SINK_COMPOSITE


def test_transport_plan_built_for_single_and_composite_with_same_code() -> None:
    single_profile = _active_single_profile()
    single_hw = OutputHardware(
        device_id=single_profile.id,
        device_label=single_profile.label,
        physical_output_count=4,
        card_id="DAC8",
    )
    single_plan = _build_outputd_transport_plan(single_hw, single_profile)
    assert single_plan.sink == TRANSPORT_SINK_SINGLE_ALSA
    assert single_plan.transport_channels == 4
    assert single_plan.channel_map == tuple(ChannelMapEntry(i, i) for i in range(4))
    assert single_plan.dac_pcms == ("hw:CARD=DAC8,DEV=0",)

    composite_hw = OutputHardware(
        device_id=DUAL_APPLE_USB_C_DAC_4CH.id,
        device_label=DUAL_APPLE_USB_C_DAC_4CH.label,
        physical_output_count=4,
        child_devices=(
            OutputChildDevice(
                "apple_dac_1",
                "apple_usb_c_dongle",
                "A",
                physical_output_indexes=(0, 1),
                card_id="AppleA",
            ),
            OutputChildDevice(
                "apple_dac_2",
                "apple_usb_c_dongle",
                "B",
                physical_output_indexes=(2, 3),
                card_id="AppleB",
            ),
        ),
    )
    composite_plan = _build_outputd_transport_plan(
        composite_hw,
        DUAL_APPLE_USB_C_DAC_4CH,
    )
    assert composite_plan.sink == TRANSPORT_SINK_COMPOSITE
    assert composite_plan.transport_channels == 4
    assert composite_plan.dac_pcms == (
        "hw:CARD=AppleA,DEV=0",
        "hw:CARD=AppleB,DEV=0",
    )


def test_dac8x_resolves_to_outputd_active_lane() -> None:
    layout = resolve_output_layout(_topology(HIFIBERRY_DAC8X.id, 8, card_id="DAC8"))

    assert layout.playback_device == ACTIVE_OUTPUTD_PLAYBACK_DEVICE
    assert layout.playback_device_source == OUTPUTD_ACTIVE_LANE_SOURCE
    assert layout.transport_channel_count == 8
    assert layout.transport_plan is not None
    assert layout.transport_plan.sink == TRANSPORT_SINK_SINGLE_ALSA
    assert layout.transport_plan.channel_map == tuple(
        ChannelMapEntry(i, i) for i in range(8)
    )
    assert layout.transport_plan.dac_pcms == ("hw:CARD=DAC8,DEV=0",)


def test_apple_usb_c_dongle_resolves_to_width_two_outputd_active_lane() -> None:
    layout = resolve_output_layout(
        _topology(APPLE_USB_C_DONGLE.id, 2, card_id="Apple")
    )

    assert layout.playback_device == ACTIVE_OUTPUTD_PLAYBACK_DEVICE
    assert layout.playback_device_source == OUTPUTD_ACTIVE_LANE_SOURCE
    assert layout.transport_channel_count == 2
    assert layout.transport_plan is not None
    assert layout.transport_plan.sink == TRANSPORT_SINK_SINGLE_ALSA
    assert layout.transport_plan.channel_map == (
        ChannelMapEntry(0, 0),
        ChannelMapEntry(1, 1),
    )
    assert layout.transport_plan.dac_pcms == ("hw:CARD=Apple,DEV=0",)


def test_no_active_lane_single_dac_is_missing_without_direct_fallback() -> None:
    layout = resolve_output_layout(_topology(GENERIC_SINGLE_DAC, 8, card_id="DAC8"))

    assert layout.playback_device is None
    assert layout.playback_device_source == MISSING_SOURCE
    assert layout.transport_channel_count == 0
    assert layout.subwoofer_supported is False
    assert layout.transport_plan is None


def test_dual_apple_uses_outputd_lane_with_composite_transport_plan() -> None:
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
    assert layout.playback_device == ACTIVE_OUTPUTD_PLAYBACK_DEVICE
    assert layout.playback_device_source == OUTPUTD_ACTIVE_LANE_SOURCE
    assert layout.transport_channel_count == 4
    assert layout.transport_plan is not None
    assert layout.transport_plan.sink == TRANSPORT_SINK_COMPOSITE
    assert all(is_stable_card_pcm(p) for p in layout.transport_plan.dac_pcms)


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


def test_output_layout_method_delegates_to_resolver() -> None:
    topo = _topology(HIFIBERRY_DAC8X.id, 8, card_id="DAC8")
    assert topo.output_layout() == resolve_output_layout(topo)


def test_output_layout_to_dict_shape_for_missing_route() -> None:
    layout = resolve_output_layout(_topology(GENERIC_SINGLE_DAC, 8, card_id="DAC8"))
    out = layout.to_dict()
    assert out["kind"] == OUTPUT_LAYOUT_KIND
    assert out["device_id"] == GENERIC_SINGLE_DAC
    assert out["card_id"] == "DAC8"
    assert out["playback_device"] is None
    assert out["playback_device_source"] == MISSING_SOURCE
    assert out["transport_channel_count"] == 0
    assert out["subwoofer_supported"] is False
    assert out["transport_plan"] is None


def test_isinstance_layout_type() -> None:
    layout = resolve_output_layout(_topology(HIFIBERRY_DAC8X.id, 8, card_id="DAC8"))
    assert isinstance(layout, OutputLayout)
