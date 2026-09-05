# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import dataclasses
import json
import shlex
import subprocess
from pathlib import Path

import pytest

from jasper.audio_hardware import dac
from jasper.audio_hardware.hat_eeprom import HatEeprom
from jasper.audio_hardware.usb_port_role import UsbPortRoleState
import jasper.cli.output_hardware as output_hardware_cli
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
    detected_hardware_adoption_precondition,
    detected_hardware_identity,
    parse_aplay_listing,
    probe_system_cards,
    topology_hardware_from_state,
)


def test_detected_hardware_identity_ignores_timestamp_and_card_number() -> None:
    first = OutputHardwareState(
        profile_id=APPLE_USB_C_DONGLE_DEVICE_ID,
        profile_label="Apple USB-C audio adapter",
        status="ready",
        physical_output_count=2,
        selected_card_id="Device",
        selected_pcm="hw:CARD=Device,DEV=0",
        child_devices=(OutputCardFact(
            card_id="Device", device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
            stable_path="/sys/devices/usb1/1-2",
        ),),
        observed_at="2026-08-18T00:00:00Z",
    )
    reenumerated = OutputHardwareState(
        profile_id=APPLE_USB_C_DONGLE_DEVICE_ID,
        profile_label="Apple USB-C audio adapter",
        status="ready",
        physical_output_count=2,
        selected_card_id="Device_1",
        selected_pcm="hw:CARD=Device_1,DEV=0",
        child_devices=(OutputCardFact(
            card_id="Device_1", device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
            stable_path="/sys/devices/usb1/1-2",
        ),),
        observed_at="2026-08-18T00:01:00Z",
    )

    assert detected_hardware_identity(first) == detected_hardware_identity(reenumerated)


def test_detected_hardware_identity_ignores_child_card_reordering() -> None:
    children = (
        OutputCardFact(card_id="A", device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
                       stable_path="/sys/devices/usb1/1-2"),
        OutputCardFact(card_id="B", device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
                       stable_path="/sys/devices/usb1/1-3"),
    )
    first = OutputHardwareState(
        profile_id=DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
        profile_label="Dual Apple", status="ready", physical_output_count=4,
        child_devices=children,
    )
    reordered = OutputHardwareState(
        profile_id=DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
        profile_label="Dual Apple", status="ready", physical_output_count=4,
        child_devices=tuple(reversed(children)),
    )

    assert detected_hardware_identity(first) == detected_hardware_identity(reordered)


def test_detected_hardware_adoption_requires_ready_usable_state() -> None:
    missing = detected_hardware_adoption_precondition(None)
    partial = detected_hardware_adoption_precondition(OutputHardwareState(
        profile_id="unknown", profile_label="Unknown", status="partial",
        physical_output_count=0,
    ))
    ready = detected_hardware_adoption_precondition(OutputHardwareState(
        profile_id=APPLE_USB_C_DONGLE_DEVICE_ID,
        profile_label="Apple USB-C audio adapter", status="ready",
        physical_output_count=2,
    ))

    assert missing["allowed"] is False
    assert partial["allowed"] is False
    assert ready["allowed"] is True


def test_detected_hardware_identity_changes_with_readiness_or_blocker() -> None:
    ready = OutputHardwareState(
        profile_id=APPLE_USB_C_DONGLE_DEVICE_ID,
        profile_label="Apple USB-C audio adapter", status="ready",
        physical_output_count=2,
    )
    partial = OutputHardwareState(
        profile_id=APPLE_USB_C_DONGLE_DEVICE_ID,
        profile_label="Apple USB-C audio adapter", status="partial",
        physical_output_count=2,
    )
    blocked = OutputHardwareState(
        profile_id=APPLE_USB_C_DONGLE_DEVICE_ID,
        profile_label="Apple USB-C audio adapter", status="ready",
        physical_output_count=2,
        issues=({"severity": "blocker", "code": "route_missing", "message": "x"},),
    )

    assert detected_hardware_identity(ready) != detected_hardware_identity(partial)
    assert detected_hardware_identity(ready) != detected_hardware_identity(blocked)


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


_STUDIO_HAT = HatEeprom(
    vendor="HiFiBerry",
    product="StudioDAC8x",
    uuid="be3b8164-dd7b-48fc-ab27-79dd7c641980",
)
# rpi-6.18.y names every Studio-family card this, so the label carries no
# product token and no width (#2258).
_UNIFIED_STUDIO_LABEL = "Hifiberry Studio Soundcard"


def _unified_studio_sysfs(tmp_path: Path) -> tuple[Path, Path]:
    sys_class = tmp_path / "sys" / "class" / "sound"
    proc_asound = tmp_path / "proc" / "asound"
    card_dir = tmp_path / "sys" / "devices" / "platform" / "soc" / "sound" / "card1"
    sys_class.mkdir(parents=True)
    proc_asound.mkdir(parents=True)
    card_dir.mkdir(parents=True)
    (sys_class / "card1").symlink_to(card_dir)
    proc_card = proc_asound / "card1"
    proc_card.mkdir()
    (proc_card / "id").write_text("HiFiBerryStudio", encoding="utf-8")
    (proc_card / "pcm0p").mkdir()
    (proc_asound / "cards").write_text(
        f" 1 [HiFiBerryStudio]: HifiberryStudio - {_UNIFIED_STUDIO_LABEL}\n"
        f"                      {_UNIFIED_STUDIO_LABEL}\n",
        encoding="utf-8",
    )
    return sys_class, proc_asound


@pytest.mark.parametrize("discovery", ["sysfs", "aplay_fallback"])
def test_hat_eeprom_routes_the_shared_studio_name_into_the_record(
    discovery: str,
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """`python -m jasper.cli.output_hardware --write` is the record's one writer.

    Both card-discovery paths must consult the same EEPROM, and the record has
    to carry the evidence it routed on — `/state` and doctor read only what
    this publishes (#2258).
    """
    state_file = tmp_path / "output_hardware.json"
    monkeypatch.setenv("JASPER_OUTPUT_HARDWARE_STATE_PATH", str(state_file))
    monkeypatch.setattr(output_hardware_cli, "read_hat_eeprom", lambda: _STUDIO_HAT)
    if discovery == "sysfs":
        sys_class, proc_asound = _unified_studio_sysfs(tmp_path)
        monkeypatch.setenv("JASPER_SYS_CLASS_SOUND", str(sys_class))
        monkeypatch.setenv("JASPER_PROC_ASOUND", str(proc_asound))
    else:
        monkeypatch.setattr(output_hardware_cli, "probe_system_cards", lambda **_: ())
        monkeypatch.setattr(
            output_hardware_cli,
            "probe_aplay_listing",
            lambda _aplay: (
                f"hw:CARD=HiFiBerryStudio,DEV=0\n    {_UNIFIED_STUDIO_LABEL}\n"
            ),
        )

    assert output_hardware_cli.main(["--write"]) == 0

    capsys.readouterr()
    published = json.loads(state_file.read_text(encoding="utf-8"))
    assert published["profile_id"] == HIFIBERRY_DAC8X_STUDIO_DEVICE_ID
    assert published["hat_eeprom"] == {
        "vendor": "HiFiBerry",
        "product": "StudioDAC8x",
        "uuid": "be3b8164-dd7b-48fc-ab27-79dd7c641980",
    }
    assert OutputHardwareState.from_mapping(published).hat_eeprom == _STUDIO_HAT


def test_the_shared_studio_name_parks_and_publishes_a_null_hat_eeprom(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """No HAT is a published fact, so a reader can tell it from an old record."""

    state_file = tmp_path / "output_hardware.json"
    sys_class, proc_asound = _unified_studio_sysfs(tmp_path)
    monkeypatch.setenv("JASPER_OUTPUT_HARDWARE_STATE_PATH", str(state_file))
    monkeypatch.setenv("JASPER_SYS_CLASS_SOUND", str(sys_class))
    monkeypatch.setenv("JASPER_PROC_ASOUND", str(proc_asound))
    monkeypatch.setattr(output_hardware_cli, "read_hat_eeprom", lambda: None)

    assert output_hardware_cli.main(["--write"]) == 0

    capsys.readouterr()
    published = json.loads(state_file.read_text(encoding="utf-8"))
    assert published["hat_eeprom"] is None
    assert published["profile_id"] == "unknown"
    assert OutputHardwareState.from_mapping(published).hat_eeprom is None


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


@pytest.mark.parametrize(
    ("requires_same_usb_bus", "expect_blocker"),
    [(True, True), (False, False)],
)
def test_dual_apple_same_bus_gate_follows_the_composite_profile_field(
    monkeypatch: pytest.MonkeyPatch,
    requires_same_usb_bus: bool,
    expect_blocker: bool,
) -> None:
    """The mismatched-bus blocker fires only when the ARMED composite profile
    declares ``requires_same_usb_bus`` (ADR-0235 R1) — the classifier no
    longer hardcodes the check."""
    monkeypatch.setattr(
        output_hardware,
        "DUAL_APPLE_USB_C_DAC_4CH",
        dataclasses.replace(
            dac.DUAL_APPLE_USB_C_DAC_4CH,
            requires_same_usb_bus=requires_same_usb_bus,
        ),
    )

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

    has_blocker = "dual_apple_usb_topology_mismatch" in {
        issue["code"] for issue in state.issues
    }
    assert has_blocker is expect_blocker
    assert state.status == ("partial" if expect_blocker else "ready")


def test_classify_more_than_two_apple_dacs_is_not_auto_promoted() -> None:
    state = classify_output_cards([
        OutputCardFact(card_id="A", device_id=APPLE_USB_C_DONGLE_DEVICE_ID),
        OutputCardFact(card_id="A_1", device_id=APPLE_USB_C_DONGLE_DEVICE_ID),
        OutputCardFact(card_id="A_2", device_id=APPLE_USB_C_DONGLE_DEVICE_ID),
    ])

    assert state.profile_id == "unknown"
    assert state.status == "partial"
    assert "too_many_apple_dacs" in {issue["code"] for issue in state.issues}


def test_topology_path_resolves_identically_to_the_topology_module(
    monkeypatch,
) -> None:
    """Two modules spell the same topology path; the park needs them equal.

    `output_hardware` keeps its own literal on purpose — importing
    `output_topology` for a constant would cost the import-cheapness this
    module advertises, and that trade is the right one. What the trade buys is
    a drift risk that nothing pinned until the park arm existed:
    `_read_topology_hardware` reads the file through one resolver while
    `_saved_topology_requires_roleful_graph` classifies it through the other,
    so a divergence would not raise — the rolefulness lookup would simply find
    no topology, report "not roleful", and the box would quietly stop parking.

    Asserting the resolvers rather than only the literals also pins the env-var
    name, which is the other half of "these two name one file".
    """

    from jasper import output_topology

    monkeypatch.delenv("JASPER_OUTPUT_TOPOLOGY_PATH", raising=False)
    assert (
        output_hardware.DEFAULT_TOPOLOGY_PATH
        == output_topology.OUTPUT_TOPOLOGY_PATH
    )
    assert output_hardware._topology_path() == output_topology.topology_path()

    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", "/tmp/jts-elsewhere.json")
    assert output_hardware._topology_path() == output_topology.topology_path()


def _apple_child(card_id: str, serial: str, usb_path: str) -> OutputCardFact:
    return OutputCardFact(
        card_id=card_id,
        device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
        serial=serial,
        usb_path=usb_path,
        busnum="1",
        controller="xhci-hcd.0",
        endpoint_sync="SYNC",
        pcm=f"hw:CARD={card_id},DEV=0",
    )


def _saved_topology(tmp_path: Path, base, hardware: dict) -> Path:
    """Save ``base``'s speaker groups under ``hardware``.

    The groups come from the runtime-contract suite's own fixtures, so
    "roleful" and "passive" here mean exactly what ``classify_output_contract``
    means by them rather than a shape hand-written in this file — which is the
    whole point, since rolefulness is now what gates the park.
    """
    raw = base.to_dict()
    raw["hardware"] = hardware
    topology_path = tmp_path / "output_topology.json"
    topology_path.write_text(json.dumps(raw), encoding="utf-8")
    return topology_path


def _assert_saved_rolefulness(topology_path: Path, expected: bool) -> None:
    """Prove the fixture is what its name claims before a test relies on it.

    Not ceremony: an invalid topology loads soft to an empty draft, which is
    NOT roleful, so a malformed fixture silently turns a park test into a
    no-op that still passes for the wrong reason. That happened once here —
    `"outputs": []` against `physical_output_count: 4` failed validation, and
    the tests only caught it because the park assertions were downstream.

    The group check is the other half, and it is the half that matters on the
    `expected=False` side: an empty draft is not roleful either, so asserting
    "not roleful" alone would pass for the very malformation this guard exists
    to catch — leaving the passive fixture, which is what pins the SF-1 carve
    out, unprotected.
    """

    from jasper.output_topology import load_output_topology

    topology = load_output_topology(topology_path)
    assert topology.speaker_groups, (
        f"{topology_path} loaded with no speaker groups — the fixture is "
        "malformed and soft-loaded to an empty draft"
    )
    assert output_hardware._saved_topology_requires_roleful_graph(
        topology_path
    ) is expected


def _roleful_composite_topology(tmp_path: Path) -> Path:
    """A commissioned active 2-way across both children — needs per-driver DSP."""
    from tests.test_active_speaker_runtime_contract import _active_topology

    path = _saved_topology(
        tmp_path,
        _active_topology("stereo", "active_2_way"),
        _saved_composite_hardware(),
    )
    _assert_saved_rolefulness(path, True)
    return path


def _passive_composite_topology(tmp_path: Path) -> Path:
    """A composite whose declared speakers all sit on child A's outputs.

    The live jts.local shape: `kind == "composite"` but full-range passive, so
    `requires_roleful_graph` is False and child B carries no declared channel.
    Unplugging B must not park a working stereo.
    """
    from tests.test_active_speaker_runtime_contract import _full_range_stereo

    path = _saved_topology(
        tmp_path,
        _full_range_stereo(),
        _saved_composite_hardware(),
    )
    _assert_saved_rolefulness(path, False)
    return path


def _saved_composite_hardware() -> dict:
    return {
        "device_id": DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
        "device_label": "Dual Apple USB-C DAC 4-channel pair",
        "physical_output_count": 4,
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
    }


def test_saved_composite_with_one_child_present_is_never_ready(
    tmp_path: Path,
) -> None:
    """A declared composite missing a child must not read as a stereo DAC.

    The surviving dongle classifies on its own as an ordinary full-range
    two-output device, and ``ready`` is the one word every consumer reads as
    "safe to drive this speaker with". Downstream layers do refuse a flat
    full-range graph on a roleful topology, so today's outcome is quiet rather
    than dangerous — but nothing states a reason. The record has to fail closed
    and name the child that is gone.
    """

    topology_path = _roleful_composite_topology(tmp_path)
    cards = [_apple_child("A", "left", "1-1")]
    observed = classify_output_cards(cards)
    assert observed.status == "ready"
    assert observed.profile_id == APPLE_USB_C_DONGLE_DEVICE_ID

    state = output_hardware.apply_saved_topology_policy(
        observed,
        cards,
        topology_path=topology_path,
    )

    assert state.status == "partial"
    assert state.profile_id == APPLE_USB_C_DONGLE_DEVICE_ID
    blockers = [
        issue for issue in state.issues if issue["severity"] == "blocker"
    ]
    assert [issue["code"] for issue in blockers] == [
        "saved_composite_partially_present"
    ]
    # The reason names the child that is gone, not just that something is.
    assert "right" in blockers[0]["message"]
    assert "left" not in blockers[0]["message"]
    # `/state` and the wizard's adopt affordance both read this record.
    assert output_hardware.detected_hardware_adoption_precondition(
        state
    )["allowed"] is False


def test_saved_passive_composite_missing_a_child_still_plays(
    tmp_path: Path,
) -> None:
    """`kind == "composite"` is not permission to park a working stereo.

    A passive composite can put every declared speaker on one child's outputs,
    which is the live jts.local shape. The other child carries no declared
    channel, so losing it costs the household nothing — and `jasper-doctor`
    already calibrates exactly this mismatch as warn rather than fail
    (`check_active_speaker_output_hardware`). Rolefulness, not compositeness,
    is the gate.
    """

    topology_path = _passive_composite_topology(tmp_path)
    cards = [_apple_child("A", "left", "1-1")]
    observed = classify_output_cards(cards)

    state = output_hardware.apply_saved_topology_policy(
        observed,
        cards,
        topology_path=topology_path,
    )

    assert state == observed
    assert state.status == "ready"
    assert state.issues == ()


def test_saved_composite_with_both_children_present_is_untouched(
    tmp_path: Path,
) -> None:
    topology_path = _roleful_composite_topology(tmp_path)
    cards = [
        _apple_child("A", "left", "1-1"),
        _apple_child("A_1", "right", "1-2"),
    ]
    observed = classify_output_cards(cards)

    state = output_hardware.apply_saved_topology_policy(
        observed,
        cards,
        topology_path=topology_path,
    )

    assert state == observed
    assert state.profile_id == DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID
    assert state.status == "ready"


def _saved_single_hardware() -> dict:
    return {
        "device_id": APPLE_USB_C_DONGLE_DEVICE_ID,
        "device_label": "Apple USB-C audio adapter",
        "physical_output_count": 2,
        "card_id": "A",
    }


def test_saved_single_topology_keeps_full_range_stereo_ready(
    tmp_path: Path,
) -> None:
    """Full-range stereo is a legal shape for a saved passive/solo speaker."""

    from tests.test_active_speaker_runtime_contract import _full_range_stereo

    topology_path = _saved_topology(
        tmp_path, _full_range_stereo(), _saved_single_hardware()
    )
    cards = [_apple_child("A", "left", "1-1")]
    observed = classify_output_cards(cards)

    state = output_hardware.apply_saved_topology_policy(
        observed,
        cards,
        topology_path=topology_path,
    )

    assert state == observed
    assert state.status == "ready"


def test_saved_single_topology_is_untouched_when_the_observed_dac_differs(
    tmp_path: Path,
) -> None:
    """A ROLEFUL saved single whose hardware was swapped is still not ours.

    The sibling test above saves and observes the same profile, so it would
    pass even without the composite guard. This one mismatches a roleful saved
    topology against a different DAC — the case that makes the guard
    load-bearing. Saved/attached mismatch on a non-composite is
    `jasper-doctor`'s to report, not this policy's to park.

    The topology must be **roleful and valid** or this test proves nothing: a
    mono active 2-way fits the dongle's two outputs, where a stereo one would
    not, and an over-wide topology loads soft to a non-roleful empty draft that
    the rolefulness gate would catch instead of the composite guard.
    """

    from tests.test_active_speaker_runtime_contract import _active_topology

    topology_path = _saved_topology(
        tmp_path, _active_topology("mono", "active_2_way"), _saved_single_hardware()
    )
    _assert_saved_rolefulness(topology_path, True)
    cards = [
        OutputCardFact(
            card_id="DAC8",
            device_id=HIFIBERRY_DAC8X_DEVICE_ID,
            label="HiFiBerry DAC8x",
            pcm="hw:CARD=DAC8,DEV=0",
        )
    ]
    observed = classify_output_cards(cards)
    assert observed.profile_id == HIFIBERRY_DAC8X_DEVICE_ID

    state = output_hardware.apply_saved_topology_policy(
        observed,
        cards,
        topology_path=topology_path,
    )

    assert state == observed
    assert state.issues == ()


def test_unsaved_topology_keeps_todays_classification(tmp_path: Path) -> None:
    """A speaker with no saved topology yet still adopts what it sees."""

    cards = [_apple_child("A", "left", "1-1")]
    observed = classify_output_cards(cards)

    state = output_hardware.apply_saved_topology_policy(
        observed,
        cards,
        topology_path=tmp_path / "output_topology.json",
    )

    assert state == observed
    assert state.status == "ready"


def test_saved_composite_with_no_output_hardware_keeps_missing_status(
    tmp_path: Path,
) -> None:
    """Both children gone already parks; it gains the reason, not a new status.

    ``missing`` is a truer word than ``partial`` for "no output hardware at
    all", so the policy only downgrades a ``ready`` observation.
    """

    topology_path = _roleful_composite_topology(tmp_path)
    observed = classify_output_cards([])

    state = output_hardware.apply_saved_topology_policy(
        observed,
        [],
        topology_path=topology_path,
    )

    assert state.status == "missing"
    codes = [issue["code"] for issue in state.issues]
    assert codes == ["saved_composite_partially_present"]
    assert "left" in state.issues[0]["message"]
    assert "right" in state.issues[0]["message"]


def test_reason_never_names_a_child_that_is_physically_present(
    tmp_path: Path,
) -> None:
    """The diagnosis is diffed against observed CARDS, not the record's children.

    Attach a registered single DAC beside both dongles and the classified
    record's `child_devices` is that DAC alone — diffing against it would
    report both Apple children missing while they are plugged in. The one
    surface this policy exists to produce must not misname hardware.
    """

    topology_path = _roleful_composite_topology(tmp_path)
    cards = [
        _apple_child("A", "left", "1-1"),
        _apple_child("A_1", "right", "1-2"),
        OutputCardFact(
            card_id="DAC8",
            device_id=HIFIBERRY_DAC8X_DEVICE_ID,
            label="HiFiBerry DAC8x",
            pcm="hw:CARD=DAC8,DEV=0",
        ),
    ]
    observed = classify_output_cards(cards)
    # The third DAC wins classification, so the record's children are not the
    # Apple pair at all — the exact shape that produced the false diagnosis.
    assert observed.profile_id == HIFIBERRY_DAC8X_DEVICE_ID
    assert [child.card_id for child in observed.child_devices] == ["DAC8"]

    state = output_hardware.apply_saved_topology_policy(
        observed,
        cards,
        topology_path=topology_path,
    )

    blockers = [
        issue for issue in state.issues
        if issue["code"] == "saved_composite_partially_present"
    ]
    assert len(blockers) == 1
    message = blockers[0]["message"]
    # Pin the TRUE sentence for this shape. Both children are attached, so the
    # remediation is to remove the interloper — NOT to reconnect anything, and
    # certainly not the inverse claim that nothing could be matched.
    assert "every declared child device is attached" in message
    assert "detach it" in message
    assert "missing child devices" not in message
    assert "no saved child device could be matched" not in message
    assert "left" not in message
    assert "right" not in message


def test_reason_says_so_when_the_topology_declares_no_children(
    tmp_path: Path,
) -> None:
    """A roleful composite with no declared children is its own sentence.

    Reachable: a composite `hardware` block with `child_devices` absent or
    empty still loads and is still roleful, so the park arm runs with nothing
    to diff. "No child matched" would be vacuously true here and actively
    misleading in the sibling shape above, which is why they are separate.
    """

    from tests.test_active_speaker_runtime_contract import _active_topology

    hardware = _saved_composite_hardware()
    del hardware["child_devices"]
    topology_path = _saved_topology(
        tmp_path, _active_topology("stereo", "active_2_way"), hardware
    )
    _assert_saved_rolefulness(topology_path, True)
    cards = [_apple_child("A", "left", "1-1")]

    state = output_hardware.apply_saved_topology_policy(
        classify_output_cards(cards),
        cards,
        topology_path=topology_path,
    )

    message = state.issues[-1]["message"]
    assert "declares no child devices" in message
    assert "missing child devices" not in message
    assert "every declared child device is attached" not in message


def test_published_record_carries_the_partial_composite_reason(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """`python -m jasper.cli.output_hardware --write` is the record's one writer.

    The reconciler and `/state` both read what this publishes, so the policy
    has to be applied here rather than by each consumer.
    """

    topology_path = _roleful_composite_topology(tmp_path)
    state_file = tmp_path / "output_hardware.json"
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    monkeypatch.setenv("JASPER_OUTPUT_HARDWARE_STATE_PATH", str(state_file))
    monkeypatch.setattr(
        output_hardware_cli,
        "probe_system_cards",
        lambda **_kwargs: (_apple_child("A", "left", "1-1"),),
    )

    assert output_hardware_cli.main(["--write"]) == 0

    capsys.readouterr()
    published = json.loads(state_file.read_text(encoding="utf-8"))
    assert published["status"] == "partial"
    assert published["profile_id"] == APPLE_USB_C_DONGLE_DEVICE_ID
    assert [issue["code"] for issue in published["issues"]] == [
        "saved_composite_partially_present"
    ]


def test_active_dac_profile_id_reads_only_the_reconciler_record(
    tmp_path, monkeypatch,
) -> None:
    from jasper.output_hardware import (
        active_dac_profile_id,
        load_state,
        published_dac_id,
        write_state,
    )

    path = tmp_path / "output_hardware.json"
    # The env publication names a DAC throughout; the resolver never reads it.
    monkeypatch.setenv("JASPER_AUDIO_DAC_ID", HIFIBERRY_DAC8X_DEVICE_ID)

    assert active_dac_profile_id(path) is None
    write_state(
        OutputHardwareState(
            profile_id="unknown", profile_label="", status="missing",
            physical_output_count=0,
        ),
        path,
    )
    assert active_dac_profile_id(path) is None
    assert load_state(path).observed_profile_id is None
    # A single DAC needs BOTH ready status AND a selected card — mirrors the
    # bash reconciler's own `apply_observed_single_policy` gate bit for bit.
    # OBSERVED does not care; the record already names the hardware it saw.
    write_state(
        OutputHardwareState(
            profile_id=APPLE_USB_C_DONGLE_DEVICE_ID, profile_label="Apple",
            status="ready", physical_output_count=2,
        ),
        path,
    )
    assert active_dac_profile_id(path) is None
    assert load_state(path).observed_profile_id == APPLE_USB_C_DONGLE_DEVICE_ID
    write_state(
        OutputHardwareState(
            profile_id=APPLE_USB_C_DONGLE_DEVICE_ID, profile_label="Apple",
            status="ready", physical_output_count=2, selected_card_id="A",
        ),
        path,
    )
    assert active_dac_profile_id(path) == APPLE_USB_C_DONGLE_DEVICE_ID
    assert load_state(path).observed_profile_id == APPLE_USB_C_DONGLE_DEVICE_ID
    # The reconciler's rule: a single DAC counts as ACTIVE only while ready;
    # OBSERVED does not care — the record still names the hardware it saw.
    write_state(
        OutputHardwareState(
            profile_id=APPLE_USB_C_DONGLE_DEVICE_ID, profile_label="Apple",
            status="partial", physical_output_count=2, selected_card_id="A",
        ),
        path,
    )
    assert active_dac_profile_id(path) is None
    assert load_state(path).observed_profile_id == APPLE_USB_C_DONGLE_DEVICE_ID
    # The dual-Apple composite counts as ACTIVE as soon as it is named, parked
    # or not, so observed and active agree here.
    write_state(
        OutputHardwareState(
            profile_id=DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID, profile_label="Dual",
            status="partial", physical_output_count=4,
        ),
        path,
    )
    assert active_dac_profile_id(path) == DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID
    assert (
        load_state(path).observed_profile_id
        == DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID
    )

    assert published_dac_id({}) == "unknown"
    assert (
        published_dac_id({"JASPER_AUDIO_DAC_ID": HIFIBERRY_DAC8X_DEVICE_ID})
        == HIFIBERRY_DAC8X_DEVICE_ID
    )


# The `jasper.cli.output_hardware --env` contract (ADR-0235 R2). Every name
# here is a variable `deploy/bin/jasper-audio-hardware-reconcile` reads after
# `eval`; a rename that lands on only one side has to fail here rather than
# read as an empty string in the shell.
_ENV_CONTRACT_KEYS = {
    "OBSERVED_OUTPUT_PROFILE_ID",
    "OBSERVED_OUTPUT_PROFILE_STATUS",
    "OBSERVED_OUTPUT_SELECTED_CARD_ID",
    "OBSERVED_OUTPUT_CHILD_DEVICE_IDS",
    "OBSERVED_OUTPUT_APPLE_CARD_IDS",
    "OBSERVED_OUTPUT_BLOCKER_CODES",
    "OBSERVED_OUTPUT_RECORD_CHANGED",
    "OBSERVED_OUTPUT_USB_MANAGEMENT_TRANSPORT_AVAILABLE",
    "OBSERVED_OUTPUT_DUAL_MAPPING_OK",
    "OBSERVED_OUTPUT_DUAL_MAPPING_REASON",
    "OBSERVED_OUTPUT_DUAL_ORDER_SOURCE",
    "OBSERVED_OUTPUT_DUAL_DAC_A_PCM",
    "OBSERVED_OUTPUT_DUAL_DAC_B_PCM",
}


@pytest.mark.parametrize(
    "card_id",
    ["A", "two words", "it's \"quoted\"", "$(touch pwned) `id` ; rm -rf /"],
    ids=["plain", "space", "quotes", "injection"],
)
def test_env_emitter_hands_bash_the_whole_contract_and_nothing_it_must_parse(
    card_id: str, tmp_path: Path,
) -> None:
    """What Python quotes, bash evals — for every key, whatever the card is.

    A card id is hardware-supplied text that reaches a shell owner, so the
    round trip is asserted through a real `bash -u` eval rather than by
    re-reading the quoting rule. `-u` also proves the emitter defines every
    key: a missing one is an unbound-variable failure, not an empty string.
    """
    card = OutputCardFact(
        card_id=card_id,
        label="Apple USB-C to 3.5mm Headphone Jack Adapter",
        device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
        vendor_id="05ac",
        product_id="110a",
        pcm=f"hw:CARD={card_id},DEV=0",
    )
    state = classify_output_cards((card,))
    payload = output_hardware_cli.env_lines(state, (card,), record_changed=True)

    emitted = {}
    for line in payload.splitlines():
        key, _, quoted = line.partition("=")
        parts = shlex.split(quoted)
        emitted[key] = parts[0] if parts else ""
    assert set(emitted) == _ENV_CONTRACT_KEYS
    assert emitted["OBSERVED_OUTPUT_SELECTED_CARD_ID"] == card_id
    assert emitted["OBSERVED_OUTPUT_APPLE_CARD_IDS"] == card_id
    assert emitted["OBSERVED_OUTPUT_RECORD_CHANGED"] == "1"

    keys = sorted(_ENV_CONTRACT_KEYS)
    reader = "; ".join(f'printf "%s\\n" "${{{key}}}"' for key in keys)
    seen = subprocess.run(
        ["bash", "-uc", f'eval "$1"; {reader}', "bash", payload],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert seen.returncode == 0, seen.stderr
    assert seen.stdout.splitlines() == [emitted[key] for key in keys]
    assert not (tmp_path / "pwned").exists()
