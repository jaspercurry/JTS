from __future__ import annotations

import json
from pathlib import Path

from jasper.output_hardware import (
    APPLE_USB_C_DONGLE_DEVICE_ID,
    DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
    HIFIBERRY_DAC8X_STUDIO_DEVICE_ID,
    OutputCardFact,
    classify_output_cards,
    dual_apple_runtime_mapping,
    parse_aplay_listing,
    probe_system_cards,
    topology_hardware_from_state,
)


def test_parse_aplay_listing_classifies_known_output_cards() -> None:
    cards = parse_aplay_listing("""
hw:CARD=A,DEV=0
    Apple USB-C to 3.5mm Headphone Jack, USB Audio
hw:CARD=DAC8XStudio,DEV=0
    HiFiBerry DAC8x Studio, USB Audio
""")

    assert [card.card_id for card in cards] == ["A", "DAC8XStudio"]
    assert cards[0].device_id == APPLE_USB_C_DONGLE_DEVICE_ID
    assert cards[1].device_id == HIFIBERRY_DAC8X_STUDIO_DEVICE_ID


def test_probe_system_cards_uses_usb_device_path_as_stable_path(
    tmp_path: Path,
) -> None:
    sys_class = tmp_path / "sys" / "class" / "sound"
    proc_asound = tmp_path / "proc" / "asound"
    usb_device = (
        tmp_path / "sys" / "devices" / "platform" / "xhci-hcd.0" / "usb1" / "1-2"
    )
    card_dir = usb_device / "1-2:1.0" / "sound" / "card5"
    sys_class.mkdir(parents=True)
    proc_asound.mkdir(parents=True)
    card_dir.mkdir(parents=True)
    for name, value in {
        "idVendor": "05ac",
        "idProduct": "110a",
        "busnum": "1",
        "devpath": "1-2",
        "product": "Apple USB-C to 3.5mm Headphone Jack",
    }.items():
        (usb_device / name).write_text(value, encoding="utf-8")
    (sys_class / "card5").symlink_to(card_dir)
    proc_card = proc_asound / "card5"
    proc_card.mkdir()
    (proc_card / "id").write_text("A", encoding="utf-8")
    (proc_card / "pcm0p").mkdir()
    (proc_card / "stream0").write_text(
        "Playback:\n  Endpoint: 0x01 (SYNC)\n",
        encoding="utf-8",
    )

    (card,) = probe_system_cards(
        sys_class_sound=sys_class,
        proc_asound=proc_asound,
    )

    assert card.card_id == "A"
    assert card.stable_path == str(usb_device.resolve())
    assert "card5" not in card.stable_path
    assert card.usb_path == "1-2"


def test_classify_single_apple_as_valid_two_channel_profile() -> None:
    state = classify_output_cards([
        OutputCardFact(
            card_id="A",
            device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
            serial="DWH53530FHL2FN3AC",
            busnum="1",
            controller="xhci-hcd.0",
        )
    ])

    assert state.profile_id == APPLE_USB_C_DONGLE_DEVICE_ID
    assert state.status == "ready"
    assert state.physical_output_count == 2
    assert state.selected_card_id == "A"


def test_classify_dual_apple_as_exact_four_channel_profile_on_same_bus() -> None:
    state = classify_output_cards([
        OutputCardFact(
            card_id="A",
            device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
            serial="DWH53530FHL2FN3AC",
            usb_path="usb1/1-2",
            busnum="1",
            controller="xhci-hcd.0",
            endpoint_sync="SYNC",
        ),
        OutputCardFact(
            card_id="A_1",
            device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
            serial="DWH53530FLL2FN3A3",
            usb_path="usb1/1-1",
            busnum="1",
            controller="xhci-hcd.0",
            endpoint_sync="SYNC",
        ),
    ])
    hardware = topology_hardware_from_state(state)

    assert state.profile_id == DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID
    assert state.status == "ready"
    assert state.physical_output_count == 4
    assert state.selected_card_id is None
    assert state.issues == ()
    assert hardware["device_id"] == DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID
    assert hardware["child_devices"][0]["physical_output_indexes"] == [0, 1]
    assert hardware["child_devices"][1]["physical_output_indexes"] == [2, 3]


def test_dual_apple_runtime_mapping_uses_saved_topology_order(
    tmp_path: Path,
) -> None:
    state = classify_output_cards([
        OutputCardFact(
            card_id="B",
            device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
            serial="right",
            usb_path="1-1",
            busnum="1",
            controller="xhci-hcd.0",
            endpoint_sync="SYNC",
            pcm="hw:CARD=B,DEV=0",
        ),
        OutputCardFact(
            card_id="A",
            device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
            serial="left",
            usb_path="1-2",
            busnum="1",
            controller="xhci-hcd.0",
            endpoint_sync="SYNC",
            pcm="hw:CARD=A,DEV=0",
        ),
    ])
    topology_path = tmp_path / "output_topology.json"
    topology_path.write_text(
        json.dumps({
            "artifact_schema_version": 1,
            "kind": "jts_output_topology",
            "topology_id": "dual_apple",
            "name": "Dual Apple",
            "status": "ready",
            "hardware": {
                "device_id": DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
                "device_label": "Dual Apple USB-C DAC 4-channel pair",
                "physical_output_count": 4,
                "outputs": [],
                "child_devices": [
                    {
                        "child_id": "left",
                        "device_id": APPLE_USB_C_DONGLE_DEVICE_ID,
                        "device_label": "Apple USB-C audio adapter",
                        "serial": "left",
                        "physical_output_indexes": [0, 1],
                    },
                    {
                        "child_id": "right",
                        "device_id": APPLE_USB_C_DONGLE_DEVICE_ID,
                        "device_label": "Apple USB-C audio adapter",
                        "serial": "right",
                        "physical_output_indexes": [2, 3],
                    },
                ],
            },
            "speaker_groups": [],
            "routing": {},
            "safety": {},
        }),
        encoding="utf-8",
    )

    mapping = dual_apple_runtime_mapping(state, topology_path=topology_path)

    assert mapping.ok is True
    assert mapping.order_source == "saved_topology"
    assert [child.pcm for child in mapping.child_devices] == [
        "hw:CARD=A,DEV=0",
        "hw:CARD=B,DEV=0",
    ]


def test_dual_apple_runtime_mapping_uses_physical_usb_path_without_serials(
    tmp_path: Path,
) -> None:
    state = classify_output_cards([
        OutputCardFact(
            card_id="B",
            device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
            stable_path="/sys/devices/platform/xhci-hcd.0/usb1/1-1",
            usb_path="1-1",
            busnum="1",
            controller="xhci-hcd.0",
            endpoint_sync="SYNC",
            pcm="hw:CARD=B,DEV=0",
        ),
        OutputCardFact(
            card_id="A",
            device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
            stable_path="/sys/devices/platform/xhci-hcd.0/usb1/1-2",
            usb_path="1-2",
            busnum="1",
            controller="xhci-hcd.0",
            endpoint_sync="SYNC",
            pcm="hw:CARD=A,DEV=0",
        ),
    ])
    topology_path = tmp_path / "output_topology.json"
    topology_path.write_text(
        json.dumps({
            "artifact_schema_version": 1,
            "kind": "jts_output_topology",
            "topology_id": "dual_apple",
            "name": "Dual Apple",
            "status": "ready",
            "hardware": {
                "device_id": DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
                "device_label": "Dual Apple USB-C DAC 4-channel pair",
                "physical_output_count": 4,
                "outputs": [],
                "child_devices": [
                    {
                        "child_id": "left",
                        "device_id": APPLE_USB_C_DONGLE_DEVICE_ID,
                        "device_label": "Apple USB-C audio adapter",
                        "stable_path": "/sys/devices/platform/xhci-hcd.0/usb1/1-2",
                        "usb_path": "1-2",
                        "physical_output_indexes": [0, 1],
                    },
                    {
                        "child_id": "right",
                        "device_id": APPLE_USB_C_DONGLE_DEVICE_ID,
                        "device_label": "Apple USB-C audio adapter",
                        "stable_path": "/sys/devices/platform/xhci-hcd.0/usb1/1-1",
                        "usb_path": "1-1",
                        "physical_output_indexes": [2, 3],
                    },
                ],
            },
            "speaker_groups": [],
            "routing": {},
            "safety": {},
        }),
        encoding="utf-8",
    )

    mapping = dual_apple_runtime_mapping(state, topology_path=topology_path)

    assert mapping.ok is True
    assert mapping.order_source == "saved_topology"
    assert [child.pcm for child in mapping.child_devices] == [
        "hw:CARD=A,DEV=0",
        "hw:CARD=B,DEV=0",
    ]


def test_dual_apple_runtime_mapping_blocks_saved_topology_identity_mismatch(
    tmp_path: Path,
) -> None:
    state = classify_output_cards([
        OutputCardFact(
            card_id="A",
            device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
            serial="observed-a",
            usb_path="1-1",
            busnum="1",
            controller="xhci-hcd.0",
            endpoint_sync="SYNC",
            pcm="hw:CARD=A,DEV=0",
        ),
        OutputCardFact(
            card_id="B",
            device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
            serial="observed-b",
            usb_path="1-2",
            busnum="1",
            controller="xhci-hcd.0",
            endpoint_sync="SYNC",
            pcm="hw:CARD=B,DEV=0",
        ),
    ])
    topology_path = tmp_path / "output_topology.json"
    topology_path.write_text(
        json.dumps({
            "artifact_schema_version": 1,
            "kind": "jts_output_topology",
            "topology_id": "dual_apple",
            "name": "Dual Apple",
            "status": "ready",
            "hardware": {
                "device_id": DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
                "device_label": "Dual Apple USB-C DAC 4-channel pair",
                "physical_output_count": 4,
                "outputs": [],
                "child_devices": [
                    {
                        "child_id": "old-a",
                        "device_id": APPLE_USB_C_DONGLE_DEVICE_ID,
                        "device_label": "Apple USB-C audio adapter",
                        "serial": "old-a",
                        "usb_path": "9-1",
                        "physical_output_indexes": [0, 1],
                    },
                    {
                        "child_id": "old-b",
                        "device_id": APPLE_USB_C_DONGLE_DEVICE_ID,
                        "device_label": "Apple USB-C audio adapter",
                        "serial": "old-b",
                        "usb_path": "9-2",
                        "physical_output_indexes": [2, 3],
                    },
                ],
            },
            "speaker_groups": [],
            "routing": {},
            "safety": {},
        }),
        encoding="utf-8",
    )

    mapping = dual_apple_runtime_mapping(state, topology_path=topology_path)

    assert mapping.ok is False
    assert mapping.reason == "saved_topology_child_identity_mismatch"


def test_classify_dual_apple_blocks_wrong_usb_bus() -> None:
    state = classify_output_cards([
        OutputCardFact(
            card_id="A",
            device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
            serial="one",
            busnum="1",
            controller="xhci-hcd.0",
        ),
        OutputCardFact(
            card_id="A_1",
            device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
            serial="two",
            busnum="3",
            controller="xhci-hcd.1",
        ),
    ])

    assert state.profile_id == DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID
    assert state.status == "partial"
    assert "dual_apple_usb_topology_mismatch" in {
        issue["code"] for issue in state.issues
    }


def test_classify_dual_apple_blocks_missing_usb_topology_facts() -> None:
    state = classify_output_cards([
        OutputCardFact(
            card_id="A",
            device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
            serial="one",
            busnum="1",
            controller="xhci-hcd.0",
        ),
        OutputCardFact(
            card_id="A_1",
            device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
            serial="two",
        ),
    ])

    assert state.profile_id == DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID
    assert state.status == "partial"
    assert "dual_apple_usb_topology_unknown" in {
        issue["code"] for issue in state.issues
    }


def test_classify_more_than_two_apple_dacs_is_not_auto_promoted() -> None:
    state = classify_output_cards([
        OutputCardFact(card_id="A", device_id=APPLE_USB_C_DONGLE_DEVICE_ID),
        OutputCardFact(card_id="A_1", device_id=APPLE_USB_C_DONGLE_DEVICE_ID),
        OutputCardFact(card_id="A_2", device_id=APPLE_USB_C_DONGLE_DEVICE_ID),
    ])

    assert state.profile_id == "unknown"
    assert state.status == "partial"
    assert "too_many_apple_dacs" in {issue["code"] for issue in state.issues}
