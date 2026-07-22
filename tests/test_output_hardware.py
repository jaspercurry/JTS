# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from pathlib import Path

from jasper.audio_hardware import dac
from jasper.audio_hardware.usb_port_role import UsbPortRoleState
import jasper.output_hardware as output_hardware
from jasper.output_hardware import (
    APPLE_USB_C_DONGLE_DEVICE_ID,
    DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
    HIFIBERRY_DAC8X_DEVICE_ID,
    HIFIBERRY_DAC8X_STUDIO_DEVICE_ID,
    OutputCardFact,
    OutputHardwareState,
    classify_output_cards,
    dual_apple_runtime_mapping,
    parse_aplay_listing,
    probe_system_cards,
    topology_hardware_from_state,
)


def test_static_output_hardware_metadata_matches_dac_registry() -> None:
    profiles = {profile.id: profile for profile in dac.all_profiles()}

    assert output_hardware.SUPPORTED_DEVICE_OUTPUT_COUNTS == {
        profile.id: profile.physical_output_count
        for profile in profiles.values()
    }
    assert output_hardware.SUPPORTED_DEVICE_LABELS == {
        profile.id: profile.label
        for profile in profiles.values()
    }
    assert output_hardware.SUPPORTED_CLOCK_DOMAIN_LABELS == {
        profile.id: profile.clock_domain_label
        for profile in profiles.values()
    }
    assert dac.by_id(output_hardware.HIFIBERRY_DAC8X_STUDIO_DEVICE_ID) is not None
    assert (
        f"{output_hardware.APPLE_USB_VENDOR_ID}:"
        f"{output_hardware.APPLE_USB_PRODUCT_ID}"
    ) in dac.APPLE_USB_C_DONGLE.usb_ids


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


def test_probe_system_cards_classifies_non_usb_hifiberry_from_proc_cards(
    tmp_path: Path,
) -> None:
    sys_class = tmp_path / "sys" / "class" / "sound"
    proc_asound = tmp_path / "proc" / "asound"
    card_dir = tmp_path / "sys" / "devices" / "platform" / "soc" / "sound" / "card2"
    sys_class.mkdir(parents=True)
    proc_asound.mkdir(parents=True)
    card_dir.mkdir(parents=True)
    (sys_class / "card2").symlink_to(card_dir)
    proc_card = proc_asound / "card2"
    proc_card.mkdir()
    (proc_card / "id").write_text("sndrpihifiberry", encoding="utf-8")
    (proc_card / "pcm0p").mkdir()
    (proc_asound / "cards").write_text(
        " 2 [sndrpihifiberry]: RPi-simple - snd_rpi_hifiberry_dac8x\n"
        "                      snd_rpi_hifiberry_dac8x\n",
        encoding="utf-8",
    )

    (card,) = probe_system_cards(
        sys_class_sound=sys_class,
        proc_asound=proc_asound,
    )
    state = classify_output_cards([card])

    assert card.device_id == HIFIBERRY_DAC8X_DEVICE_ID
    assert state.profile_id == HIFIBERRY_DAC8X_DEVICE_ID
    assert state.status == "ready"
    assert state.physical_output_count == 8


def test_output_hardware_state_from_mapping_preserves_zero_apple_dac_count() -> None:
    state = OutputHardwareState.from_mapping(
        {
            "artifact_schema_version": 1,
            "kind": "jts_output_hardware_state",
            "profile_id": HIFIBERRY_DAC8X_DEVICE_ID,
            "profile_label": "HiFiBerry DAC8x",
            "status": "ready",
            "physical_output_count": 8,
            "apple_dac_count": 0,
            "child_devices": [
                {
                    "card_id": "sndrpihifiberry",
                    "device_id": HIFIBERRY_DAC8X_DEVICE_ID,
                    "label": "snd_rpi_hifiberry_dac8x",
                    "has_playback": True,
                    "pcm": "hw:CARD=sndrpihifiberry,DEV=0",
                },
            ],
            "issues": [],
        }
    )

    assert state.apple_dac_count == 0
    assert len(state.child_devices) == 1


def test_output_hardware_state_from_mapping_tolerates_bad_numeric_fields() -> None:
    state = OutputHardwareState.from_mapping(
        {
            "profile_id": HIFIBERRY_DAC8X_DEVICE_ID,
            "status": "ready",
            "physical_output_count": "not-an-int",
            "apple_dac_count": "not-an-int",
            "child_devices": [
                {
                    "card_id": "sndrpihifiberry",
                    "device_id": HIFIBERRY_DAC8X_DEVICE_ID,
                },
                {
                    "card_id": "A",
                    "device_id": APPLE_USB_C_DONGLE_DEVICE_ID,
                },
            ],
        }
    )

    assert state.physical_output_count == 0
    assert state.apple_dac_count == 1


def test_output_hardware_state_round_trips_usb_data_role() -> None:
    role = UsbPortRoleState(
        board_model="Raspberry Pi Zero 2 W Rev 1.0",
        board_topology="shared_otg_port",
        desired_role="host",
        configured_role="host",
        active_role="host",
        gadget_available=False,
        reboot_required=False,
        reason="shared_otg_usb_output_requires_host",
        decision_reason="shared_otg_usb_output_requires_host",
        management_transport_available=False,
    )
    original = OutputHardwareState(
        profile_id=APPLE_USB_C_DONGLE_DEVICE_ID,
        profile_label="Apple USB-C audio adapter",
        status="ready",
        physical_output_count=2,
        usb_data_role=role,
    )

    restored = OutputHardwareState.from_mapping(original.to_dict())

    assert restored.usb_data_role == role
    serialized_role = original.to_dict()["usb_data_role"]
    assert "gadget_capable" not in serialized_role
    assert "observed_output_profile_id" not in serialized_role


def test_output_hardware_state_rejects_malformed_usb_data_role_fail_closed() -> None:
    state = OutputHardwareState.from_mapping(
        {
            "profile_id": APPLE_USB_C_DONGLE_DEVICE_ID,
            "status": "ready",
            "physical_output_count": 2,
            "usb_data_role": {
                "board_topology": "shared_otg_port",
                "desired_role": "peripheral",
                "gadget_available": True,
            },
        }
    )

    assert state.usb_data_role is None


def test_output_hardware_state_rejects_impossible_usb_role() -> None:
    raw = {
        "board_model": "Raspberry Pi Zero 2 W Rev 1.0",
        "board_topology": "shared_otg_port",
        "desired_role": "host",
        "configured_role": "host",
        "active_role": "host",
        "gadget_available": True,
        "management_transport_available": False,
        "reboot_required": False,
        "reason": "shared_otg_defaults_host_without_i2s",
        "decision_reason": "shared_otg_defaults_host_without_i2s",
        "configured_i2s_overlays": [],
    }

    assert UsbPortRoleState.from_mapping(raw) is None


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


def test_classify_registered_single_dac_uses_profile_contract(monkeypatch) -> None:
    future = dac.DacProfile(
        id="future_balanced_dac",
        label="Future Balanced DAC",
        kind="single",
        physical_output_count=6,
        coherent_clock_domain=True,
        clock_domain_label="Single future DAC device clock",
        clock_domain_contract="single_device",
        outputd_sink="alsa",
        supported_card_matches=("future balanced",),
    )
    base_lookup = output_hardware._dac_profile_by_id

    def lookup(profile_id: str) -> dac.DacProfile | None:
        if profile_id == future.id:
            return future
        return base_lookup(profile_id)

    monkeypatch.setattr(output_hardware, "_dac_profile_by_id", lookup)

    state = classify_output_cards([
        OutputCardFact(
            card_id="FUTURE",
            label="Future Balanced DAC",
            device_id=future.id,
            pcm="hw:CARD=FUTURE,DEV=0",
        ),
        OutputCardFact(
            card_id="A",
            device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
        ),
    ])

    assert state.profile_id == future.id
    assert state.profile_label == future.label
    assert state.status == "ready"
    assert state.physical_output_count == future.physical_output_count
    assert state.selected_card_id == "FUTURE"
    assert state.selected_pcm == "hw:CARD=FUTURE,DEV=0"
    assert state.apple_dac_count == 1
    assert [child.card_id for child in state.child_devices] == ["FUTURE"]


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
