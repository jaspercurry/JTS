# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import dataclasses

import pytest

import jasper.output_hardware as output_hardware
from jasper.output_hardware import (
    active_dac_profile_id,
    load_state,
    published_dac_id,
    write_state,
)

from jasper.audio_hardware import dac
from jasper.audio_hardware.usb_port_role import UsbPortRoleState
from jasper.output_hardware import (
    APPLE_USB_C_DONGLE_DEVICE_ID,
    DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
    OutputCardFact,
    OutputHardwareState,
    classify_output_cards,
    detected_hardware_adoption_precondition,
)
from tests.output_topology_fixtures import _dual_apple_observation


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


def test_output_hardware_state_from_mapping_preserves_zero_apple_dac_count() -> None:
    state = OutputHardwareState.from_mapping(
        {
            "artifact_schema_version": 1,
            "kind": "jts_output_hardware_state",
            "profile_id": dac.HIFIBERRY_DAC8X_ID,
            "profile_label": "HiFiBerry DAC8x",
            "status": "ready",
            "physical_output_count": 8,
            "apple_dac_count": 0,
            "child_devices": [
                {
                    "card_id": "sndrpihifiberry",
                    "device_id": dac.HIFIBERRY_DAC8X_ID,
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
            "profile_id": dac.HIFIBERRY_DAC8X_ID,
            "status": "ready",
            "physical_output_count": "not-an-int",
            "apple_dac_count": "not-an-int",
            "child_devices": [
                {
                    "card_id": "sndrpihifiberry",
                    "device_id": dac.HIFIBERRY_DAC8X_ID,
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
    state = _dual_apple_observation()

    assert state.profile_id == DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID
    assert state.status == "ready"
    assert state.physical_output_count == 4
    assert state.selected_card_id is None
    assert state.issues == ()


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


def test_active_dac_profile_id_reads_only_the_reconciler_record(
    tmp_path, monkeypatch,
) -> None:

    path = tmp_path / "output_hardware.json"
    # The env publication names a DAC throughout; the resolver never reads it.
    monkeypatch.setenv("JASPER_AUDIO_DAC_ID", dac.HIFIBERRY_DAC8X_ID)

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
        published_dac_id({"JASPER_AUDIO_DAC_ID": dac.HIFIBERRY_DAC8X_ID})
        == dac.HIFIBERRY_DAC8X_ID
    )
