from __future__ import annotations

import re

import pytest

from jasper.audio_hardware import dac
from jasper.audio_hardware.dac import (
    APPLE_USB_C_DONGLE,
    APPLE_USB_C_DONGLE_ID,
    DUAL_APPLE_USB_C_DAC_4CH,
    DUAL_APPLE_USB_C_DAC_4CH_ID,
    HIFIBERRY_DAC8X,
    HIFIBERRY_DAC8X_ID,
    HIFIBERRY_DAC8X_STUDIO,
    HIFIBERRY_DAC8X_STUDIO_ID,
    DacProfile,
)


def test_registry_contains_current_output_profiles_in_stable_order() -> None:
    assert dac.all_profiles() == (
        APPLE_USB_C_DONGLE,
        HIFIBERRY_DAC8X,
        HIFIBERRY_DAC8X_STUDIO,
        DUAL_APPLE_USB_C_DAC_4CH,
    )
    assert dac.known_profile_ids() == (
        APPLE_USB_C_DONGLE_ID,
        HIFIBERRY_DAC8X_ID,
        HIFIBERRY_DAC8X_STUDIO_ID,
        DUAL_APPLE_USB_C_DAC_4CH_ID,
    )


def test_lookup_helpers_are_pure_and_unknown_safe() -> None:
    assert dac.by_id(APPLE_USB_C_DONGLE_ID) is APPLE_USB_C_DONGLE
    assert dac.by_id("unknown_usb_dac") is None
    assert dac.is_known_profile_id(HIFIBERRY_DAC8X_ID) is True
    assert dac.is_known_profile_id("unknown_usb_dac") is False
    assert dac.physical_output_count_for(DUAL_APPLE_USB_C_DAC_4CH_ID) == 4
    assert dac.physical_output_count_for("unknown_usb_dac") is None
    assert dac.clock_domain_contract_for(APPLE_USB_C_DONGLE_ID) == "single_device"
    assert (
        dac.clock_domain_contract_for(DUAL_APPLE_USB_C_DAC_4CH_ID)
        == "measured_sync_required"
    )
    assert dac.clock_domain_contract_for("unknown_usb_dac") is None
    assert dac.supports_physical_output_count(HIFIBERRY_DAC8X_ID, 8) is True
    assert dac.supports_physical_output_count(HIFIBERRY_DAC8X_ID, 4) is False
    assert dac.supports_physical_output_count("unknown_usb_dac", 2) is False


def test_apple_usb_c_dongle_profile_captures_current_mixer_policy() -> None:
    assert APPLE_USB_C_DONGLE.kind == "single"
    assert APPLE_USB_C_DONGLE.physical_output_count == 2
    assert APPLE_USB_C_DONGLE.coherent_clock_domain is True
    assert APPLE_USB_C_DONGLE.clock_domain_label == (
        "Single Apple USB audio device clock"
    )
    assert APPLE_USB_C_DONGLE.clock_domain_contract == "single_device"
    assert APPLE_USB_C_DONGLE.outputd_sink == "alsa"
    assert APPLE_USB_C_DONGLE.supports_active_outputd_lane is True
    assert APPLE_USB_C_DONGLE.usb_ids == ("05ac:110a",)
    assert APPLE_USB_C_DONGLE.supported_card_matches == ("usb-c to 3.5mm",)
    assert APPLE_USB_C_DONGLE.headphone_pinned_100 is True
    assert APPLE_USB_C_DONGLE.mixer_controls[0].name == "Headphone"
    assert APPLE_USB_C_DONGLE.mixer_controls[0].target_percent == 100
    assert APPLE_USB_C_DONGLE.mixer_controls[0].unmute is True
    assert (
        APPLE_USB_C_DONGLE.udev_rule
        == "deploy/udev/99-jasper-apple-dongle.rules"
    )


def test_hifiberry_dac8x_profiles_cover_base_and_studio_runtime_ids() -> None:
    assert HIFIBERRY_DAC8X.id == "hifiberry_dac8x"
    assert HIFIBERRY_DAC8X.label == "HiFiBerry DAC8x"
    assert HIFIBERRY_DAC8X.kind == "single"
    assert HIFIBERRY_DAC8X.physical_output_count == 8
    assert HIFIBERRY_DAC8X.coherent_clock_domain is True
    assert HIFIBERRY_DAC8X.clock_domain_label == (
        "Single HiFiBerry DAC8x device clock"
    )
    assert HIFIBERRY_DAC8X.clock_domain_contract == "single_device"
    assert HIFIBERRY_DAC8X.outputd_sink == "alsa"
    assert HIFIBERRY_DAC8X.supports_active_outputd_lane is True
    assert (
        "snd_rpi_hifiberry_dac8x(?!.*studio)"
        in HIFIBERRY_DAC8X.supported_card_matches
    )
    assert "hifiberry.*dac8x(?!.*studio)" in HIFIBERRY_DAC8X.supported_card_matches
    assert HIFIBERRY_DAC8X.validation_profile == "hifiberry_dac8x_outputd_stability"
    assert HIFIBERRY_DAC8X.dtoverlay == "hifiberry-dac8x"
    assert HIFIBERRY_DAC8X_STUDIO.id == "hifiberry_dac8x_studio"
    assert HIFIBERRY_DAC8X_STUDIO.label == "HiFiBerry DAC8x Studio"
    assert HIFIBERRY_DAC8X_STUDIO.physical_output_count == 8
    assert HIFIBERRY_DAC8X_STUDIO.clock_domain_contract == "single_device"
    assert HIFIBERRY_DAC8X_STUDIO.outputd_sink == "alsa"
    assert HIFIBERRY_DAC8X_STUDIO.validation_profile == (
        "hifiberry_dac8x_outputd_stability"
    )


def test_hifiberry_studio_match_hints_do_not_overlap_base_dac8x() -> None:
    base_label = "snd_rpi_hifiberry_dac8x, HiFiBerry DAC8x"
    studio_label = "HiFiBerry DAC8x Studio, USB Audio"
    studio_kernel_label = "snd_rpi_hifiberry_dac8x_studio"

    assert any(
        re.search(pattern, base_label, re.IGNORECASE)
        for pattern in HIFIBERRY_DAC8X.supported_card_matches
    )
    assert not any(
        re.search(pattern, studio_label, re.IGNORECASE)
        for pattern in HIFIBERRY_DAC8X.supported_card_matches
    )
    assert not any(
        re.search(pattern, studio_kernel_label, re.IGNORECASE)
        for pattern in HIFIBERRY_DAC8X.supported_card_matches
    )
    assert any(
        re.search(pattern, studio_label, re.IGNORECASE)
        for pattern in HIFIBERRY_DAC8X_STUDIO.supported_card_matches
    )
    assert dac.profile_for_card_label(base_label) is HIFIBERRY_DAC8X
    assert dac.profile_for_card_label(studio_label) is HIFIBERRY_DAC8X_STUDIO
    assert dac.profile_for_card_label(studio_kernel_label) is HIFIBERRY_DAC8X_STUDIO
    assert dac.profile_for_card_label("Mystery USB DAC") is None


def test_dual_apple_profile_is_first_class_composite_four_output_dac() -> None:
    assert DUAL_APPLE_USB_C_DAC_4CH.kind == "composite"
    assert DUAL_APPLE_USB_C_DAC_4CH.physical_output_count == 4
    assert DUAL_APPLE_USB_C_DAC_4CH.coherent_clock_domain is False
    assert DUAL_APPLE_USB_C_DAC_4CH.clock_domain_label == (
        "Dual Apple USB-C DAC pair (measured sync required)"
    )
    assert DUAL_APPLE_USB_C_DAC_4CH.clock_domain_contract == (
        "measured_sync_required"
    )
    assert DUAL_APPLE_USB_C_DAC_4CH.outputd_sink == "dual_apple"
    assert DUAL_APPLE_USB_C_DAC_4CH.child_profile_ids == (
        APPLE_USB_C_DONGLE_ID,
        APPLE_USB_C_DONGLE_ID,
    )
    assert DUAL_APPLE_USB_C_DAC_4CH.usb_ids == ("05ac:110a",)
    assert DUAL_APPLE_USB_C_DAC_4CH.requires_same_usb_bus is True
    assert DUAL_APPLE_USB_C_DAC_4CH.supports_active_outputd_lane is True
    assert DUAL_APPLE_USB_C_DAC_4CH.mixer_controls == ()
    assert dac.mixer_control_groups_for(DUAL_APPLE_USB_C_DAC_4CH_ID) == (
        APPLE_USB_C_DONGLE.mixer_controls,
        APPLE_USB_C_DONGLE.mixer_controls,
    )
    assert DUAL_APPLE_USB_C_DAC_4CH.headphone_pinned_100 is True


def test_profile_validation_rejects_bad_static_shapes() -> None:
    with pytest.raises(ValueError, match="unsupported DAC profile id"):
        DacProfile(
            id="../bad",
            label="Bad",
            kind="single",
            physical_output_count=2,
            coherent_clock_domain=True,
            clock_domain_label="Bad clock",
            clock_domain_contract="single_device",
            outputd_sink="alsa",
            supported_card_matches=("bad",),
        )

    with pytest.raises(ValueError, match="composite DAC profile needs children"):
        DacProfile(
            id="bad_composite",
            label="Bad composite",
            kind="composite",
            physical_output_count=4,
            coherent_clock_domain=False,
            clock_domain_label="Bad clock",
            clock_domain_contract="measured_sync_required",
            outputd_sink="dual_apple",
            supported_card_matches=("usb",),
            child_profile_ids=(APPLE_USB_C_DONGLE_ID,),
        )

    with pytest.raises(ValueError, match="mixer target_percent"):
        dac.MixerControl("Headphone", target_percent=101)

    with pytest.raises(ValueError, match="composite mixer controls"):
        DacProfile(
            id="bad_composite_mixer",
            label="Bad composite mixer",
            kind="composite",
            physical_output_count=4,
            coherent_clock_domain=False,
            clock_domain_label="Bad clock",
            clock_domain_contract="measured_sync_required",
            outputd_sink="dual_apple",
            supported_card_matches=("usb",),
            child_profile_ids=(APPLE_USB_C_DONGLE_ID, APPLE_USB_C_DONGLE_ID),
            mixer_controls=APPLE_USB_C_DONGLE.mixer_controls,
        )


def test_registry_children_reference_known_profiles() -> None:
    known = set(dac.known_profile_ids())
    for profile in dac.all_profiles():
        assert set(profile.child_profile_ids) <= known
