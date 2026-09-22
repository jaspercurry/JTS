# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared topology declarations and observed hardware inputs."""

from jasper.output_hardware import (
    OutputCardFact,
    OutputHardwareState,
    classify_output_cards,
)
from jasper.output_topology import (
    APPLE_USB_C_DONGLE_DEVICE_ID,
    DUAL_APPLE_ACTIVE_DEVICE_ID,
    OUTPUT_TOPOLOGY_KIND,
    OutputTopology,
)


def _base_hardware() -> dict:
    return {
        "device_id": "hifiberry_dac8x",
        "device_label": "HiFiBerry DAC8x",
        "physical_output_count": 8,
    }


def _topology(
    *,
    groups: list[dict],
    routing: dict | None = None,
    hardware: dict | None = None,
) -> OutputTopology:
    raw = {
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "living_room",
        "name": "Living room",
        "status": "draft",
        "hardware": hardware or _base_hardware(),
        "speaker_groups": groups,
        "routing": routing or {},
    }
    return OutputTopology.from_mapping(raw)


def _passive_main(
    group_id: str, kind: str, index: int, *, identity_verified: bool = False
) -> dict:
    return {
        "id": group_id,
        "label": group_id.title(),
        "kind": kind,
        "mode": "full_range_passive",
        "channels": [{
            "role": "full_range",
            "physical_output_index": index,
            "identity_verified": identity_verified,
        }],
    }


def _passive_sub_topology_raw(fc: object) -> dict:
    sub_channel: dict = {"role": "subwoofer", "physical_output_index": 2}
    if fc is not None:
        sub_channel["crossover_fc_hz"] = fc
    return {
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "bench",
        "name": "Bench",
        "status": "draft",
        "hardware": {
            "device_id": "hifiberry_dac8x",
            "device_label": "HiFiBerry DAC8x",
            "physical_output_count": 8,
            "card_id": "DAC8",
        },
        "speaker_groups": [
            {
                "id": "left",
                "label": "L",
                "kind": "left",
                "mode": "full_range_passive",
                "channels": [{"role": "full_range", "physical_output_index": 0}],
            },
            {
                "id": "right",
                "label": "R",
                "kind": "right",
                "mode": "full_range_passive",
                "channels": [{"role": "full_range", "physical_output_index": 1}],
            },
            {
                "id": "sub",
                "label": "Sub",
                "kind": "subwoofer",
                "mode": "subwoofer",
                "channels": [sub_channel],
            },
        ],
        "routing": {
            "main_left_group_id": "left",
            "main_right_group_id": "right",
            "subwoofer_group_ids": ["sub"],
        },
    }


def _fingerprint_topology() -> OutputTopology:
    return _topology(
        groups=[_passive_main("left", "left", 0), _passive_main("right", "right", 1)],
        routing={"main_left_group_id": "left", "main_right_group_id": "right"},
    )


def _dual_apple_hardware() -> dict:
    hardware = {
        "device_id": DUAL_APPLE_ACTIVE_DEVICE_ID,
        "device_label": "Dual Apple USB-C DAC 4-channel pair",
        "physical_output_count": 4,
        "child_devices": [
            {
                "child_id": "left_dac",
                "device_id": APPLE_USB_C_DONGLE_DEVICE_ID,
                "device_label": "Apple USB-C audio adapter",
                "serial": "DWH53530FHL2FN3AC",
                "card_id": "A",
                "usb_path": "usb1/1-2",
                "controller": "xhci-hcd.0",
                "physical_output_indexes": [0, 1],
            },
            {
                "child_id": "right_dac",
                "device_id": APPLE_USB_C_DONGLE_DEVICE_ID,
                "device_label": "Apple USB-C audio adapter",
                "serial": "DWH53530FLL2FN3A3",
                "card_id": "A_1",
                "usb_path": "usb1/1-1",
                "controller": "xhci-hcd.0",
                "physical_output_indexes": [2, 3],
            },
        ],
    }
    return hardware


def _dual_apple_observation(
    *,
    serial_a: str = "DWH53530FHL2FN3AC",
    serial_b: str = "DWH53530FLL2FN3A3",
    port_b: str = "usb1/1-1",
    same_bus: bool = True,
) -> OutputHardwareState:
    """Classify the two attached Apple dongles the saved fixture pins.

    ``port_b`` moves the second dongle to another port on the SAME USB bus, so
    the classifier still calls the pair ready — that is the case a re-pin's
    port anchor must reject on its own merits rather than inheriting a refusal
    from ``same_bus=False`` (which the classifier already blocks).
    """

    return classify_output_cards([
        OutputCardFact(
            card_id="A",
            device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
            serial=serial_a,
            usb_path="usb1/1-2",
            busnum="1",
            controller="xhci-hcd.0",
            endpoint_sync="SYNC",
        ),
        OutputCardFact(
            card_id="A_1",
            device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
            serial=serial_b,
            usb_path=port_b if same_bus else "usb3/3-1",
            busnum="1" if same_bus else "3",
            controller="xhci-hcd.0" if same_bus else "xhci-hcd.1",
            endpoint_sync="SYNC",
        ),
    ])
