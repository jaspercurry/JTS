# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import re
from pathlib import Path

import pytest

from jasper.audio_hardware import dac
from jasper.audio_hardware.hat_eeprom import HatEeprom, read_hat_eeprom
from jasper.camilla_config_contract import CamillaFloor
from jasper.audio_hardware.dac import (
    APPLE_USB_C_DONGLE,
    APPLE_USB_C_DONGLE_ID,
    DUAL_APPLE_USB_C_DAC_4CH,
    DUAL_APPLE_USB_C_DAC_4CH_ID,
    HIFIBERRY_DAC8X,
    HIFIBERRY_DAC8X_ID,
    HIFIBERRY_DAC8X_STUDIO,
    HIFIBERRY_DAC8X_STUDIO_ID,
    INNOMAKER_HIFI_AMP_PRO,
    INNOMAKER_HIFI_AMP_PRO_ID,
    DacProfile,
    LatencyFloor,
)

from ._hat_eeprom import write_hat_eeprom

ROOT = Path(__file__).resolve().parents[1]


def test_registry_contains_current_output_profiles_in_stable_order() -> None:
    assert dac.all_profiles() == (
        APPLE_USB_C_DONGLE,
        HIFIBERRY_DAC8X,
        HIFIBERRY_DAC8X_STUDIO,
        INNOMAKER_HIFI_AMP_PRO,
        DUAL_APPLE_USB_C_DAC_4CH,
    )


def test_lookup_helpers_are_pure_and_unknown_safe() -> None:
    assert dac.by_id(APPLE_USB_C_DONGLE_ID) is APPLE_USB_C_DONGLE
    assert dac.by_id("unknown_usb_dac") is None
    assert dac.is_known_profile_id(HIFIBERRY_DAC8X_ID) is True
    assert dac.is_known_profile_id("unknown_usb_dac") is False
    assert dac.physical_output_count_for(DUAL_APPLE_USB_C_DAC_4CH_ID) == 4
    assert dac.physical_output_count_for("unknown_usb_dac") is None
    assert dac.active_outputd_lane_channels_for(APPLE_USB_C_DONGLE_ID) == 2
    assert dac.active_outputd_lane_channels_for(HIFIBERRY_DAC8X_ID) == 8
    assert dac.active_outputd_lane_channels_for(HIFIBERRY_DAC8X_STUDIO_ID) == 8
    assert dac.active_outputd_lane_channels_for(INNOMAKER_HIFI_AMP_PRO_ID) == 2
    assert dac.active_outputd_lane_channels_for(DUAL_APPLE_USB_C_DAC_4CH_ID) == 4
    assert dac.active_outputd_lane_channels_for("unknown_usb_dac") is None
    assert dac.clock_domain_contract_for(APPLE_USB_C_DONGLE_ID) == "single_device"
    assert (
        dac.clock_domain_contract_for(DUAL_APPLE_USB_C_DAC_4CH_ID)
        == "measured_sync_required"
    )
    assert dac.clock_domain_contract_for("unknown_usb_dac") is None


def test_apple_usb_c_dongle_profile_captures_current_mixer_policy() -> None:
    assert APPLE_USB_C_DONGLE.kind == "single"
    assert APPLE_USB_C_DONGLE.physical_output_count == 2
    assert APPLE_USB_C_DONGLE.coherent_clock_domain is True
    assert APPLE_USB_C_DONGLE.clock_domain_label == (
        "Single Apple USB audio device clock"
    )
    assert APPLE_USB_C_DONGLE.clock_domain_contract == "single_device"
    assert APPLE_USB_C_DONGLE.outputd_sink == "single_alsa"
    assert APPLE_USB_C_DONGLE.supports_active_outputd_lane is True
    assert APPLE_USB_C_DONGLE.active_outputd_lane_channels == 2
    assert APPLE_USB_C_DONGLE.usb_ids == ("05ac:110a",)
    assert APPLE_USB_C_DONGLE.connection == "usb"
    assert APPLE_USB_C_DONGLE.supported_card_matches == ("usb-c to 3.5mm",)
    assert APPLE_USB_C_DONGLE.mixer_controls[0].name == "Headphone"
    assert APPLE_USB_C_DONGLE.mixer_controls[0].target_percent == 100
    assert APPLE_USB_C_DONGLE.mixer_controls[0].unmute is True


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
    assert HIFIBERRY_DAC8X.outputd_sink == "single_alsa"
    assert HIFIBERRY_DAC8X.connection == "i2s"
    # Stage 2: the DAC-agnostic transport carries a coherent 8-channel single
    # DAC, so the DAC8x declares the active outputd lane at its full width.
    assert HIFIBERRY_DAC8X.supports_active_outputd_lane is True
    assert HIFIBERRY_DAC8X.active_outputd_lane_channels == 8
    # No explicit channel map => the transport builds an identity map.
    assert HIFIBERRY_DAC8X.dac_channel_map is None
    # Exactly the literal `rpi-simple-soundcard.c` emits as this board's
    # `.card_name`, and nothing fuzzier — the family patterns it replaced
    # matched labels no driver produces while swallowing Studio silicon
    # (#2250).
    assert HIFIBERRY_DAC8X.supported_card_matches == (
        r"\bsnd_rpi_hifiberry_dac8x\b",
    )
    assert HIFIBERRY_DAC8X.validation_profile == "hifiberry_dac8x_outputd_stability"
    assert HIFIBERRY_DAC8X.supports_active_crossover_commissioning is True
    assert HIFIBERRY_DAC8X.dtoverlay == "hifiberry-dac8x"
    assert HIFIBERRY_DAC8X_STUDIO.id == "hifiberry_dac8x_studio"
    assert HIFIBERRY_DAC8X_STUDIO.supports_active_crossover_commissioning is False
    assert HIFIBERRY_DAC8X_STUDIO.label == "HiFiBerry DAC8x Studio"
    assert HIFIBERRY_DAC8X_STUDIO.physical_output_count == 8
    assert HIFIBERRY_DAC8X_STUDIO.clock_domain_contract == "single_device"
    assert HIFIBERRY_DAC8X_STUDIO.outputd_sink == "single_alsa"
    assert HIFIBERRY_DAC8X_STUDIO.connection == "i2s"
    assert HIFIBERRY_DAC8X_STUDIO.supports_active_outputd_lane is True
    assert HIFIBERRY_DAC8X_STUDIO.active_outputd_lane_channels == 8
    assert HIFIBERRY_DAC8X_STUDIO.validation_profile == (
        "hifiberry_dac8x_outputd_stability"
    )
    # The Studio's own overlay, not the base board's. `configured_i2s_overlays()`
    # intersects config.txt against these declarations, so the previous
    # `hifiberry-dac8x` here made a correctly-configured Studio box read as
    # having no I2S HAT at all.
    assert HIFIBERRY_DAC8X_STUDIO.dtoverlay == "hifiberry-studio-dac8x"
    assert HIFIBERRY_DAC8X.dtoverlay != HIFIBERRY_DAC8X_STUDIO.dtoverlay


def test_hifiberry_studio_match_hints_do_not_overlap_base_dac8x() -> None:
    """A real Studio board must never be classified as the base DAC8x (#2250).

    The labels here are the ones the kernel actually emits, not plausible ones.
    Base: `rpi-simple-soundcard.c` sets `.card_name =
    "snd_rpi_hifiberry_dac8x"`. Studio: `hifiberry_studio_dac8x.c` sets
    `.name = "Hifiberry Studio DAC8x"` and its probe renames to "HiFiBerry
    Studio DAC8x" / "... Pro". Both put "Studio" BEFORE "DAC8x", which is
    exactly the ordering the old trailing `(?!.*studio)` lookaheads failed to
    exclude — every one of these Studio labels used to resolve to the base
    profile and inherit its `chip_aec_qualification="approved"` and S32_LE
    edge.

    `label` is `product or proc_description or card_id`
    (`output_hardware.probe_system_cards`); an I2S HAT has no USB `product`,
    so the realistic value is the joined /proc/asound/cards description. Both
    the bare card name and that joined line are covered below.
    """
    base_labels = (
        "snd_rpi_hifiberry_dac8x",
        " 2 [sndrpihifiberry]: RPi-simple - snd_rpi_hifiberry_dac8x"
        " snd_rpi_hifiberry_dac8x",
    )
    studio_labels = (
        # Kernel card names, both capitalizations the driver ships.
        "HiFiBerry Studio DAC8x",
        "Hifiberry Studio DAC8x",
        # The realistic driver-derived proc description — the case the
        # pre-#2250 version of this test explicitly declined to cover.
        " 2 [HiFiBerryStudio]: HifiberryStudio - HiFiBerry Studio DAC8x"
        " HiFiBerry Studio DAC8x",
        # HiFiBerry's own product-naming order, and both run-together slug
        # forms an alphanumeric-stripped ALSA card id can present.
        "HiFiBerry DAC8x Studio, USB Audio",
        "StudioDAC8x",
        "DAC8XStudio",
    )

    for label in base_labels:
        assert dac.profile_for_card_label(label) is HIFIBERRY_DAC8X, label
        assert not any(
            re.search(pattern, label, re.IGNORECASE)
            for pattern in HIFIBERRY_DAC8X_STUDIO.supported_card_matches
        ), label
    for label in studio_labels:
        assert dac.profile_for_card_label(label) is HIFIBERRY_DAC8X_STUDIO, label
        assert not any(
            re.search(pattern, label, re.IGNORECASE)
            for pattern in HIFIBERRY_DAC8X.supported_card_matches
        ), label

    assert dac.profile_for_card_label("Mystery USB DAC") is None


def test_studio_dac8x_pro_parks_rather_than_borrowing_the_studio_profile() -> None:
    """The PRO is a different board and must not inherit this row (#2250).

    `hifiberry-studio-dac8x-pro-overlay.dts` targets `i2s_clk_consumer` and
    sets `clk-provider`, so the CARD drives the I2S clocks — the opposite of
    the non-Pro Studio's `i2s_clk_producer`. Silently handing it the non-Pro
    row's clock-domain contract and overlay is the same defect class #2250
    fixed one board over. No Pro exists in the fleet, so "unknown" (which
    parks the output owner loudly) is the honest answer.

    The run-together slug forms are covered too, not just the spaced kernel
    label: a `\\b`-bounded exclusion (`(?!.*\\bpro\\b)`) cannot see "pro" in
    "StudioDAC8xPro" — "x" and "P" are both word characters, so no boundary
    exists between them — and would silently fail to exclude exactly the
    slug shape this profile deliberately matches for the non-Pro board.
    """
    for label in (
        "HiFiBerry Studio DAC8x Pro",
        " 3 [HiFiBerryStudio]: HifiberryStudio - HiFiBerry Studio DAC8x Pro"
        " HiFiBerry Studio DAC8x Pro",
        "StudioDAC8xPro",
        "DAC8XStudioPro",
    ):
        assert dac.profile_for_card_label(label) is None, label


_DAC8X_STUDIO_HAT = HatEeprom(
    vendor="HiFiBerry",
    product="StudioDAC8x",
    uuid="be3b8164-dd7b-48fc-ab27-79dd7c641980",
)

# `label` is `product or proc_description or card_id`
# (`output_hardware.probe_system_cards`), so both the bare kernel card name
# and the joined /proc/asound/cards description are realistic inputs.
_UNIFIED_STUDIO_PROC_LABEL = (
    " 2 [HiFiBerryStudio]: HifiberryStudio - Hifiberry Studio Soundcard"
    " Hifiberry Studio Soundcard"
)


@pytest.mark.parametrize(
    ("label", "hat_product", "expected_id"),
    [
        # The base driver stack keeps its own row even on Studio silicon:
        # a profile keys the DRIVER STACK, and the EEPROM only ever
        # disambiguates a label more than one stack emits. jts3 is exactly
        # this case — Studio silicon on the base `hifiberry-dac8x` overlay.
        ("snd_rpi_hifiberry_dac8x", None, HIFIBERRY_DAC8X_ID),
        ("snd_rpi_hifiberry_dac8x", "StudioDAC8x", HIFIBERRY_DAC8X_ID),
        # An unambiguous Studio label needs no EEPROM and is not overridden.
        ("HiFiBerry Studio DAC8x", None, HIFIBERRY_DAC8X_STUDIO_ID),
        ("HiFiBerry Studio DAC8x", "StudioDAC8x", HIFIBERRY_DAC8X_STUDIO_ID),
        # rpi-6.18.y's `hifiberry_studio.c` names the 8-channel Studio DAC8x
        # and the 2-channel Studio Digi/AES alike — no product token, no
        # width — so on the label alone this name parks rather than risking a
        # Digi being driven as an 8-channel board (#2258).
        ("Hifiberry Studio Soundcard", None, None),
        (_UNIFIED_STUDIO_PROC_LABEL, None, None),
        ("Hifiberry Studio Soundcard", "StudioDAC8x", HIFIBERRY_DAC8X_STUDIO_ID),
        (_UNIFIED_STUDIO_PROC_LABEL, "StudioDAC8x", HIFIBERRY_DAC8X_STUDIO_ID),
        # Whole-string comparison, not a substring test: the Pro is a
        # different board with the opposite I2S clock role.
        ("Hifiberry Studio Soundcard", "StudioDAC8xPro", None),
        ("HiFiBerry Studio DAC8x Pro", "StudioDAC8x", None),
        # A HAT node that publishes no product routes nothing, so a partial
        # read is never mistaken for evidence.
        ("Hifiberry Studio Soundcard", "", None),
        # An EEPROM never invents a route for a label no row claims, and the
        # dangerous sibling parks with or without one.
        ("Hifiberry Studio Digi", None, None),
        ("Hifiberry Studio Digi", "StudioDAC8x", None),
        ("Mystery USB DAC", "StudioDAC8x", None),
    ],
)
def test_card_label_routing_matrix_over_hat_eeprom_evidence(
    label: str,
    hat_product: str | None,
    expected_id: str | None,
) -> None:
    hat = (
        None
        if hat_product is None
        else HatEeprom(vendor="HiFiBerry", product=hat_product, uuid="uuid")
    )
    profile = dac.profile_for_card_label(label, hat=hat)
    assert (profile.id if profile is not None else None) == expected_id


@pytest.mark.parametrize(
    ("product", "expected_id"),
    [
        (HIFIBERRY_DAC8X_STUDIO.hat_products[0], HIFIBERRY_DAC8X_STUDIO_ID),
        # Whole-string, case-insensitive match (dac._declares_hat_product).
        (HIFIBERRY_DAC8X_STUDIO.hat_products[0].lower(), HIFIBERRY_DAC8X_STUDIO_ID),
        # A different board — must not inherit this row (#2250).
        ("StudioDAC8xPro", None),
        # A HAT node that publishes no product routes nothing, and neither
        # does the absence of a HAT (None, as in the label matrix above).
        ("", None),
        ("unregistered_hat_product", None),
        (None, None),
    ],
)
def test_profile_for_hat_matches_the_registrys_declared_hat_products(
    product: str | None, expected_id: str | None
) -> None:
    hat = (
        None
        if product is None
        else HatEeprom(vendor="HiFiBerry", product=product, uuid="uuid")
    )
    profile = dac.profile_for_hat(hat)
    assert (profile.id if profile is not None else None) == expected_id


def test_hat_eeprom_reader_reports_none_when_no_hat_node_exists(
    tmp_path: Path,
) -> None:
    assert read_hat_eeprom(tmp_path / "absent") is None
    empty = tmp_path / "empty"
    empty.mkdir()
    assert read_hat_eeprom(empty) is None


def test_hat_eeprom_reader_strips_devicetree_nul_terminators(
    tmp_path: Path,
) -> None:
    write_hat_eeprom(tmp_path, product="StudioDAC8x")

    assert read_hat_eeprom(tmp_path) == _DAC8X_STUDIO_HAT


def test_hat_eeprom_reader_keeps_a_partially_published_node(
    tmp_path: Path,
) -> None:
    """A blank field is an answer, not a failure — it just matches no row."""

    write_hat_eeprom(tmp_path, uuid=None)
    partial = read_hat_eeprom(tmp_path)

    assert partial == HatEeprom(vendor="HiFiBerry", product="", uuid="")
    assert dac.profile_for_card_label(
        "Hifiberry Studio Soundcard", hat=partial
    ) is None


# One or more realistic ALSA labels per single-kind profile. The walking guard
# below asserts this covers the registry exactly, so a new single profile fails
# here until its labels are declared and shown not to collide with any other
# profile's patterns.
_PROFILE_LABEL_SAMPLES: dict[str, tuple[str, ...]] = {
    "apple_usb_c_dongle": ("Apple USB-C to 3.5mm Headphone Jack, USB Audio",),
    "hifiberry_dac8x": (
        "snd_rpi_hifiberry_dac8x",
        " 2 [sndrpihifiberry]: RPi-simple - snd_rpi_hifiberry_dac8x"
        " snd_rpi_hifiberry_dac8x",
    ),
    "hifiberry_dac8x_studio": (
        "HiFiBerry Studio DAC8x",
        "HiFiBerry DAC8x Studio, USB Audio",
        "StudioDAC8x",
        # Gated on the HAT EEPROM at lookup time, but still exclusive here:
        # no other row may claim the shared 6.18 Studio name either.
        "Hifiberry Studio Soundcard",
    ),
    "innomaker_hifi_amp_pro": (
        "snd_rpi_merus_amp",
        "Merus Audio AMP ma120x0p-amp-0",
    ),
}


def test_every_single_profile_label_matches_exactly_one_profile() -> None:
    """No ALSA label may be claimed by two single-kind profiles.

    `profile_for_card_label` returns the FIRST registry match, so an overlap is
    invisible at runtime — the losing profile just silently never applies, and
    its hardware inherits the winner's whole declaration set. That is #2250
    exactly: the base DAC8x's family regexes claimed every Studio label, so the
    Studio row's `chip_aec_qualification="needs_calibration"` never reached the
    board it describes.

    This walks the registry rather than pinning the one pair that broke, so the
    next profile added with an over-broad pattern fails here instead of shipping
    a second silent inheritance.
    """
    single_ids = {
        profile.id for profile in dac.all_profiles() if profile.kind == "single"
    }
    assert set(_PROFILE_LABEL_SAMPLES) == single_ids, (
        "every single-kind profile needs label samples; missing "
        f"{sorted(single_ids - set(_PROFILE_LABEL_SAMPLES))}, unknown "
        f"{sorted(set(_PROFILE_LABEL_SAMPLES) - single_ids)}"
    )

    checked = 0
    for owner_id, labels in _PROFILE_LABEL_SAMPLES.items():
        # Per-profile non-vacuity: the global `checked >= len(single_ids)`
        # check below is satisfied even if ONE profile's tuple is emptied,
        # as long as every other profile still contributes >=2 labels — it
        # cannot catch a single profile silently losing its coverage.
        assert labels, f"{owner_id} needs at least one sample label"
        for label in labels:
            matching = [
                profile.id
                for profile in dac.all_profiles()
                if profile.kind == "single"
                and any(
                    re.search(pattern, label, re.IGNORECASE)
                    for pattern in (
                        profile.supported_card_matches
                        + profile.eeprom_gated_card_matches
                    )
                )
            ]
            assert matching == [owner_id], (
                f"{label!r} should match only {owner_id!r}, matched {matching}"
            )
            checked += 1
    # Non-vacuity: an empty sample map would satisfy every assertion above by
    # never running one.
    assert checked >= len(single_ids), f"expected >= {len(single_ids)}, saw {checked}"


def test_innomaker_hifi_amp_pro_declares_the_width_two_active_i2s_shape() -> None:
    profile = INNOMAKER_HIFI_AMP_PRO

    assert profile.kind == "single"
    assert profile.physical_output_count == 2
    assert profile.connection == "i2s"
    assert profile.dtoverlay == "merus-amp"
    assert profile.usb_ids == ()
    # THE FLIP: this board carries the active-output lane at width 2 — the
    # same shape the single Apple USB-C dongle already runs (one coherent
    # ALSA device, one mono active 2-way).
    assert profile.supports_active_outputd_lane is True
    assert profile.active_outputd_lane_channels == 2
    assert profile.is_coherent_single() is True
    # The lane can never ask for more than the board physically has.
    assert profile.active_outputd_lane_channels <= profile.physical_output_count
    # No explicit channel map => the transport builds an identity map.
    assert profile.dac_channel_map is None
    # The declared format the raw `hw:` open lands on. Unchanged by the flip,
    # and load-bearing: outputd requests it and parks at exit 78 if the device
    # installs something else.
    assert profile.final_edge_format == "S32_LE"
    assert (
        dac.profile_for_card_label(
            "snd_rpi_merus_amp, Merus Audio Amp ma120x0p-amp-0"
        )
        is profile
    )


def test_innomaker_lane_does_not_claim_the_narrower_launch_scopes() -> None:
    """Declaring the transport lane is not a claim about measured scopes.

    Two deliberately-narrower capabilities stay off, each with its own
    qualification evidence that this board has not been through:

    * ``supports_active_crossover_commissioning`` gates ONLY the AUTOMATIC
      measured summed-region capture service
      (``CommissioningCaptureService._current``), which is DAC8x-scoped. The
      guided/manual commissioning flow reads no DacProfile field beyond the
      route capability — which is exactly how the Apple USB-C dongle, also
      ``False`` here, ran a commissioned mono active 2-way.
    * ``chip_aec_qualification`` needs per-profile timing calibration.

    The registry validator would ACCEPT commissioning=True post-flip
    (``is_coherent_single() and supports_active_outputd_lane``), so nothing but
    this test stops it drifting on without the measurements behind it.
    """

    assert INNOMAKER_HIFI_AMP_PRO.supports_active_crossover_commissioning is False
    assert APPLE_USB_C_DONGLE.supports_active_crossover_commissioning is False
    assert APPLE_USB_C_DONGLE.supports_active_outputd_lane is True
    assert INNOMAKER_HIFI_AMP_PRO.chip_aec_qualification == "needs_calibration"


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
    assert DUAL_APPLE_USB_C_DAC_4CH.connection == "usb"
    assert DUAL_APPLE_USB_C_DAC_4CH.child_profile_ids == (
        APPLE_USB_C_DONGLE_ID,
        APPLE_USB_C_DONGLE_ID,
    )
    assert DUAL_APPLE_USB_C_DAC_4CH.usb_ids == ("05ac:110a",)
    assert DUAL_APPLE_USB_C_DAC_4CH.requires_same_usb_bus is True
    assert DUAL_APPLE_USB_C_DAC_4CH.supports_active_outputd_lane is True
    assert DUAL_APPLE_USB_C_DAC_4CH.active_outputd_lane_channels == 4
    assert DUAL_APPLE_USB_C_DAC_4CH.mixer_controls == ()
    assert dac.mixer_control_groups_for(DUAL_APPLE_USB_C_DAC_4CH_ID) == (
        APPLE_USB_C_DONGLE.mixer_controls,
        APPLE_USB_C_DONGLE.mixer_controls,
    )


def test_profile_validation_rejects_bad_static_shapes() -> None:
    with pytest.raises(ValueError, match="unsupported DAC connection"):
        DacProfile(
            id="bad_connection",
            label="Bad",
            kind="single",
            physical_output_count=2,
            coherent_clock_domain=True,
            clock_domain_label="Bad clock",
            clock_domain_contract="single_device",
            outputd_sink="alsa",
            supported_card_matches=("bad",),
            connection="spi",  # type: ignore[arg-type]
        )

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

    with pytest.raises(ValueError, match="active_outputd_lane_channels is required"):
        DacProfile(
            id="bad_active_missing_width",
            label="Bad active",
            kind="single",
            physical_output_count=2,
            coherent_clock_domain=True,
            clock_domain_label="Bad clock",
            clock_domain_contract="single_device",
            outputd_sink="alsa",
            supported_card_matches=("bad",),
            supports_active_outputd_lane=True,
        )

    with pytest.raises(
        ValueError, match="commissioning requires one coherent active-output device"
    ):
        DacProfile(
            id="bad_composite_commissioning",
            label="Bad composite commissioning",
            kind="composite",
            physical_output_count=4,
            coherent_clock_domain=False,
            clock_domain_label="Two clocks",
            clock_domain_contract="measured_sync_required",
            outputd_sink="dual_apple",
            supported_card_matches=("usb",),
            child_profile_ids=(APPLE_USB_C_DONGLE_ID, APPLE_USB_C_DONGLE_ID),
            supports_active_outputd_lane=True,
            active_outputd_lane_channels=4,
            supports_active_crossover_commissioning=True,
        )


def _active_single(**overrides: object) -> DacProfile:
    """A minimal valid single coherent active-lane DAC for channel-map tests."""

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


def _identity_map(width: int) -> tuple[dac.ChannelMapEntry, ...]:
    return tuple(dac.ChannelMapEntry(i, i) for i in range(width))


def test_is_coherent_single_predicate() -> None:
    # The single-PCM-transport shape: one device, one clock.
    assert HIFIBERRY_DAC8X.is_coherent_single() is True
    assert APPLE_USB_C_DONGLE.is_coherent_single() is True
    # A composite of two independent-clock devices is not.
    assert DUAL_APPLE_USB_C_DAC_4CH.is_coherent_single() is False


def test_dac_channel_map_accepts_valid_permutations() -> None:
    assert _active_single(dac_channel_map=_identity_map(4)).dac_channel_map == (
        _identity_map(4)
    )
    # A non-identity permutation onto distinct, in-range physical channels.
    swapped = _active_single(
        dac_channel_map=(
            dac.ChannelMapEntry(0, 1),
            dac.ChannelMapEntry(1, 0),
            dac.ChannelMapEntry(2, 3),
            dac.ChannelMapEntry(3, 2),
        )
    )
    assert len(swapped.dac_channel_map or ()) == 4
    # Active lane narrower than physical outputs: map the 4 lanes onto a subset
    # of the 8 physical channels (distinct, in range).
    narrow = _active_single(
        physical_output_count=8,
        active_outputd_lane_channels=4,
        dac_channel_map=(
            dac.ChannelMapEntry(0, 0),
            dac.ChannelMapEntry(1, 2),
            dac.ChannelMapEntry(2, 4),
            dac.ChannelMapEntry(3, 6),
        ),
    )
    assert len(narrow.dac_channel_map or ()) == 4


def test_dac_channel_map_validation_is_fail_closed() -> None:
    # A map only means something for a DAC with an active lane.
    with pytest.raises(ValueError, match="dac_channel_map requires"):
        DacProfile(
            id="map_no_lane",
            label="x",
            kind="single",
            physical_output_count=2,
            coherent_clock_domain=True,
            clock_domain_label="c",
            clock_domain_contract="single_device",
            outputd_sink="alsa",
            supported_card_matches=("x",),
            dac_channel_map=_identity_map(2),
        )
    # One entry per active-lane channel (width is 4 here).
    with pytest.raises(ValueError, match="one entry per active-lane channel"):
        _active_single(dac_channel_map=_identity_map(2))
    # camilla_out_index must be a clean 0..width-1 permutation (no gap/dup).
    with pytest.raises(ValueError, match="camilla_out_index values must be"):
        _active_single(
            dac_channel_map=(
                dac.ChannelMapEntry(0, 0),
                dac.ChannelMapEntry(0, 1),
                dac.ChannelMapEntry(2, 2),
                dac.ChannelMapEntry(3, 3),
            )
        )
    # No two lanes may share a physical channel.
    with pytest.raises(ValueError, match="same"):
        _active_single(
            dac_channel_map=(
                dac.ChannelMapEntry(0, 0),
                dac.ChannelMapEntry(1, 0),
                dac.ChannelMapEntry(2, 2),
                dac.ChannelMapEntry(3, 3),
            )
        )
    # physical_dac_channel must be within physical_output_count.
    with pytest.raises(ValueError, match="exceeds physical_output_count"):
        _active_single(
            dac_channel_map=(
                dac.ChannelMapEntry(0, 0),
                dac.ChannelMapEntry(1, 1),
                dac.ChannelMapEntry(2, 2),
                dac.ChannelMapEntry(3, 9),
            )
        )
    # The entry itself rejects negative indices.
    with pytest.raises(ValueError, match="camilla_out_index must be"):
        dac.ChannelMapEntry(-1, 0)
    with pytest.raises(ValueError, match="physical_dac_channel must be"):
        dac.ChannelMapEntry(0, -1)

    with pytest.raises(ValueError, match="cannot exceed physical_output_count"):
        DacProfile(
            id="bad_active_too_wide",
            label="Bad active",
            kind="single",
            physical_output_count=2,
            coherent_clock_domain=True,
            clock_domain_label="Bad clock",
            clock_domain_contract="single_device",
            outputd_sink="alsa",
            supported_card_matches=("bad",),
            supports_active_outputd_lane=True,
            active_outputd_lane_channels=4,
        )

    with pytest.raises(ValueError, match="requires supports_active_outputd_lane"):
        DacProfile(
            id="bad_inactive_width",
            label="Bad active",
            kind="single",
            physical_output_count=2,
            coherent_clock_domain=True,
            clock_domain_label="Bad clock",
            clock_domain_contract="single_device",
            outputd_sink="alsa",
            supported_card_matches=("bad",),
            active_outputd_lane_channels=2,
        )


def test_registry_children_reference_known_profiles() -> None:
    known = {profile.id for profile in dac.all_profiles()}
    for profile in dac.all_profiles():
        assert set(profile.child_profile_ids) <= known


# --- #27 per-DAC latency floor ------------------------------------------------


def test_latency_floor_rejects_dac_buffer_below_two_periods() -> None:
    with pytest.raises(ValueError, match="outputd_dac_buffer_frames must be >= 2 x"):
        LatencyFloor(outputd_period_frames=256, outputd_dac_buffer_frames=511)


def test_latency_floor_rejects_nonpositive_values() -> None:
    with pytest.raises(ValueError, match="must be > 0"):
        LatencyFloor(outputd_period_frames=0, outputd_dac_buffer_frames=512)


def test_apple_dongle_declares_the_measured_floors() -> None:
    # The codified floors must match the measured Apple-dongle floors. The exact
    # 4x Camilla target (1024) and outputd 64/128 were too tight on jts.local.
    assert APPLE_USB_C_DONGLE.latency_floor == LatencyFloor(
        outputd_period_frames=128,
        outputd_dac_buffer_frames=256,
    )
    assert APPLE_USB_C_DONGLE.camilla_floor == CamillaFloor(
        chunksize=256, target_level=1536,
    )


def test_floor_accessors_round_trip_by_profile_id() -> None:
    floor = dac.latency_floor_for(APPLE_USB_C_DONGLE_ID)
    camilla = dac.camilla_floor_for(APPLE_USB_C_DONGLE_ID)
    assert floor is not None and camilla is not None
    assert (floor.outputd_period_frames, floor.outputd_dac_buffer_frames) == (128, 256)
    assert (camilla.chunksize, camilla.target_level) == (256, 1536)


def test_floor_accessors_are_none_for_undeclared_and_unknown() -> None:
    # A DAC that declares no floor keeps the shipped default — None is the
    # non-breaking signal the reconciler and the emitters read as "use default".
    assert dac.latency_floor_for(HIFIBERRY_DAC8X_STUDIO_ID) is None
    assert dac.camilla_floor_for(HIFIBERRY_DAC8X_STUDIO_ID) is None
    assert dac.latency_floor_for("no_such_dac") is None
    assert dac.camilla_floor_for("no_such_dac") is None


def test_innomaker_declares_the_jts4_outputd_and_camilla_floors() -> None:
    # jts4 (Pi Zero 2 W + InnoMaker HiFi AMP Pro): period 128 / dac_buffer 256 —
    # the pair both other declaring profiles use — took 1 DAC xrun in 5 minutes
    # here, and 512 took zero in 5 minutes plus zero in a 3-minute run through
    # the armed ring. CamillaDSP's pair is the ring-clamped 256/1024 the ring
    # clamp (#3542) scaled this board's former 1024/4096 down to; jts4 has run
    # it since, CamillaDSP-validated.
    assert INNOMAKER_HIFI_AMP_PRO.latency_floor == LatencyFloor(
        outputd_period_frames=128,
        outputd_dac_buffer_frames=512,
    )
    assert INNOMAKER_HIFI_AMP_PRO.camilla_floor == CamillaFloor(
        chunksize=256, target_level=1024,
    )


def test_innomaker_floor_period_is_what_makes_the_ring_reachable() -> None:
    """The 128 is not cosmetic: it is the whole ring-eligibility fact.

    Ring A's slot is fan-in's COMPILE-TIME ``RING_SLOT_FRAMES`` with no env
    override, and the conf.d renderer refuses any other period, so a declared
    floor reaches shm_ring only by EQUALLING it. Pinned against the constant
    rather than the literal 128, so moving one moves this test with it.
    """
    from jasper.fanin_coupling import RING_SLOT_FRAMES

    floor = dac.latency_floor_for(INNOMAKER_HIFI_AMP_PRO_ID)
    assert floor is not None
    assert floor.outputd_period_frames == RING_SLOT_FRAMES
    # And the deeper DAC ring this board needed is a real divergence from the
    # two profiles measured on Pi 5 silicon, not a copy of them.
    assert floor.outputd_dac_buffer_frames == 512
    assert APPLE_USB_C_DONGLE.latency_floor is not None
    assert (
        floor.outputd_dac_buffer_frames
        != APPLE_USB_C_DONGLE.latency_floor.outputd_dac_buffer_frames
    )


def test_dac8x_declares_the_soak_validated_floors() -> None:
    # The R7a jts3 soak validated the Apple dongle's four values on I2S
    # silicon: three 30-min windows, zero DAC xruns, DAC presentation latency
    # 63.833 -> 5.167 ms. Same numbers as the dongle, independently measured —
    # this pins the DECLARATION, not the transfer.
    assert HIFIBERRY_DAC8X.latency_floor == LatencyFloor(
        outputd_period_frames=128,
        outputd_dac_buffer_frames=256,
    )
    assert HIFIBERRY_DAC8X.camilla_floor == CamillaFloor(
        chunksize=256, target_level=1536,
    )


# --- final-edge format (PR-1, final-edge format program) ---------------------


def test_final_edge_format_is_declared_and_within_allowed_set() -> None:
    for profile in dac.all_profiles():
        assert profile.final_edge_format in ("S16_LE", "S24_3LE", "S32_LE"), (
            f"{profile.id}: {profile.final_edge_format!r}"
        )


def _rust_dac_format_arms() -> set[str]:
    """The values outputd's own parse accepts for ``JASPER_OUTPUTD_DAC_FORMAT``.

    Read out of the Rust match arms rather than restated, so the test below
    compares the two IMPLEMENTATIONS instead of comparing the registry to a copy
    of itself. Same idiom as
    ``tests/test_wifi_profile_hardening_contract.py``: read the fact out of each
    owner and compare, rather than comparing one owner to a copy of itself.

    The unset/blank arm (``"" | "S16_LE"``) contributes ``S16_LE`` only: the empty
    string is outputd's default-when-absent, and no ``DacProfile`` can carry it
    (``__post_init__`` rejects it), so it is not part of the shared vocabulary.
    Its presence IS asserted, though — it is how this parser proves it found the
    real arm block and not some other match.
    """
    config_rs = (ROOT / "rust" / "jasper-outputd" / "src" / "config.rs").read_text(
        encoding="utf-8"
    )
    anchor = 'let declared_dac_format = match env_str("JASPER_OUTPUTD_DAC_FORMAT", "").trim() {'
    assert anchor in config_rs, (
        "could not locate outputd's JASPER_OUTPUTD_DAC_FORMAT parse in "
        "rust/jasper-outputd/src/config.rs — if the match was reshaped, update "
        "this parser; do not delete the contract it feeds"
    )
    # Everything between the match's `{` and its catch-all `other =>` arm: the
    # value arms and nothing else.
    block = config_rs.split(anchor, 1)[1].split("other =>", 1)[0]
    literals = re.findall(r'"([^"]*)"', block)
    assert "" in literals, (
        "outputd's DAC-format parse no longer has an unset/blank arm; this parser "
        "is probably reading the wrong block"
    )
    arms = {literal for literal in literals if literal}
    assert arms, (
        "the JASPER_OUTPUTD_DAC_FORMAT arm block parsed to an EMPTY set — the "
        "regex found nothing, which would make the contract below vacuously true"
    )
    return arms


def _registry_final_edge_formats() -> set[str]:
    """The values ``DacProfile.__post_init__`` accepts, read out of its tuple."""
    src = (ROOT / "jasper" / "audio_hardware" / "dac.py").read_text(encoding="utf-8")
    match = re.search(r"if self\.final_edge_format not in \(([^)]*)\):", src)
    assert match is not None, (
        "could not locate the final_edge_format validation tuple in "
        "jasper/audio_hardware/dac.py"
    )
    values = set(re.findall(r'"([^"]*)"', match.group(1)))
    assert values, (
        "the final_edge_format validation tuple parsed to an EMPTY set — the "
        "regex found nothing, which would make the contract below vacuously true"
    )
    return values


def test_the_accepted_final_edge_format_set_is_exactly_what_outputd_parses() -> None:
    """The accepted SET itself, not just "every profile is inside it" — and it is
    compared against outputd's ACTUAL parse, not against prose claiming parity.

    The set-vs-membership distinction is what this test was built for, and the
    gap it was built to cover has since closed from the other side: when nothing
    declared ``S24_3LE``, removing it from ``DacProfile.__post_init__``'s tuple
    left every other assertion in this file green while silently making the
    registry unable to express the width outputd's packed write path exists for.
    Now that ``APPLE_USB_C_DONGLE`` declares it, that same removal raises
    ``ValueError`` at import and takes most of this module down with it. The
    equality assertion below is still the one that catches the *reverse* edit —
    adding a value the registry validates and Rust cannot parse — which no
    membership check reaches at all.

    The cross-owner comparison is what keeps it HONEST. One fact lives in two
    places — this tuple, and ``config.rs``'s ``JASPER_OUTPUTD_DAC_FORMAT`` match
    arms — with no shared code between a Python dataclass and a Rust daemon, and
    until now only a docstring linked them. Both drift directions matter, so this
    is EQUALITY, not the superset the rate-match alias contract uses:

    * **Registry accepts more than Rust parses** → a profile declares it,
      ``jasper-audio-hardware-reconcile`` emits it verbatim into
      ``JASPER_OUTPUTD_DAC_FORMAT``, outputd's parse bails, and the FINAL-OUTPUT
      OWNER parks at exit 78 — a silent speaker after a routine deploy.
    * **Rust parses more than the registry validates** → unreachable dead
      vocabulary. Harmless in itself, but it hides the drift direction above:
      whichever side is broader, the two owners have stopped agreeing about one
      fact. The tuple and the parse move together.
    """
    rust_arms = _rust_dac_format_arms()
    registry_values = _registry_final_edge_formats()

    accepted = ("S16_LE", "S24_3LE", "S32_LE")
    # The literal is the non-vacuity proof that both parsers above found real
    # blocks rather than nothing.
    assert rust_arms == set(accepted), (
        "outputd's JASPER_OUTPUTD_DAC_FORMAT parse arms changed — update "
        "DacProfile.__post_init__'s tuple in jasper/audio_hardware/dac.py AND "
        "this literal set. Both edit sites, every time: the registry emitting a "
        "value outputd rejects parks jasper-outputd at exit 78 and silences the "
        f"speaker. Parsed from config.rs: {sorted(rust_arms)}"
    )
    assert registry_values == rust_arms, (
        "the DAC edge-format vocabulary drifted between its two owners. "
        f"jasper/audio_hardware/dac.py accepts {sorted(registry_values)}; "
        f"rust/jasper-outputd/src/config.rs parses {sorted(rust_arms)}. "
        f"Registry-only (emitted but unparseable -> parked final-output owner at "
        f"exit 78): {sorted(registry_values - rust_arms)}. "
        f"Rust-only (unreachable dead vocabulary): {sorted(rust_arms - registry_values)}"
    )

    for value in accepted:
        # Constructed, not asserted about: __post_init__ is the gate.
        profile = DacProfile(
            id="accepted_final_edge_format",
            label="Accepted",
            kind="single",
            physical_output_count=2,
            coherent_clock_domain=True,
            clock_domain_label="Accepted clock",
            clock_domain_contract="single_device",
            outputd_sink="alsa",
            supported_card_matches=("accepted",),
            final_edge_format=value,
        )
        assert profile.final_edge_format == value

    # And nothing adjacent sneaks in. `S24_LE` is the sharp one: same 24 bits in a
    # FOUR-byte word, one character from the accepted packed spelling, and a
    # stride outputd's packed staging is not built at.
    for rejected in ("S24_LE", "S24_3BE", "s24_3le", "S32_BE", "FLOAT_LE"):
        assert rejected not in accepted
        with pytest.raises(ValueError, match="unsupported final_edge_format"):
            DacProfile(
                id="rejected_final_edge_format",
                label="Rejected",
                kind="single",
                physical_output_count=2,
                coherent_clock_domain=True,
                clock_domain_label="Rejected clock",
                clock_domain_contract="single_device",
                outputd_sink="alsa",
                supported_card_matches=("rejected",),
                final_edge_format=rejected,
            )


def test_final_edge_format_matches_known_hardware() -> None:
    # wide-output-path PR-8 b3: the Apple dongle's USB descriptor advertises
    # exactly S16_LE and S24_3LE at 48 kHz / 2ch, and a live
    # `aplay -D hw:A -f S24_3LE` open succeeded on jts.local (2026-08-08).
    # S24_3LE is the widest edge this silicon has — the dongle exposes no 32-bit
    # width at all.
    assert APPLE_USB_C_DONGLE.final_edge_format == "S24_3LE"
    # wide-output-path PR-7: HiFiBerry DAC8x is a measured S32 edge (jts3
    # `aplay --dump-hw-params` open test, 2026-08-07) — declaring it moves
    # outputd's i32 program spine straight through with zero narrowing.
    assert HIFIBERRY_DAC8X.final_edge_format == "S32_LE"
    # DAC8x Studio stays at the safe default: same DAC-chip family as the base
    # DAC8x above, but a DIFFERENT overlay and driver (#2250 —
    # `hifiberry-studio-dac8x`, `hifiberry_studio_dac8x.c`), and no lab unit
    # exists to run the same hardware open-test, so this program does not flip
    # it on inference alone. See the profile's own comment in
    # jasper/audio_hardware/dac.py for exactly what evidence would flip it.
    assert HIFIBERRY_DAC8X_STUDIO.final_edge_format == "S16_LE"
    assert INNOMAKER_HIFI_AMP_PRO.final_edge_format == "S32_LE"
    # The composite's declaration became load-bearing when outputd started
    # requesting it on BOTH children (wide-output-path PR-5): every live
    # dual-Apple box opens its two dongles at exactly this value, so an
    # unintended flip here is an unintended change to real hardware. It stays
    # S16_LE while its child declares S24_3LE above — see
    # test_a_composite_never_declares_a_width_its_transport_refuses for why that
    # divergence is the correct state and not drift.
    assert DUAL_APPLE_USB_C_DAC_4CH.final_edge_format == "S16_LE"


def test_a_composite_never_declares_a_width_its_transport_refuses() -> None:
    """No composite may declare ``S24_3LE``: the paired sink cannot drive it.

    This is the Python half of a two-sided guard. The Rust half is
    ``a_composite_child_edge_of_packed_24_is_refused_not_silently_narrowed``
    plus
    ``a_packed_24_composite_declaration_parks_the_unit_instead_of_restart_looping``
    in ``rust/jasper-outputd/src/alsa_backend.rs``, which pin that
    ``ChildPeriods::new`` refuses a packed-24 child edge and that
    ``PairedCompositeSink::new`` turns the refusal into an EX_CONFIG 78 park
    before either dongle opens. The refusal is correct behaviour; a composite
    declaration that triggers it is a silent speaker shipped as data, so the
    registry must never produce one.

    ``ChildPeriods`` holds an i16 pair or an i32 pair and
    ``deinterleave_4ch_to_dual_stereo<T: ChildEdgeSample>`` is generic over the
    sample type — Rust has no 3-byte integer for ``T`` to be, which is why the
    packed edge needed its own byte-staging write path on the single-DAC sink
    (#2249) and why the paired sink has no equivalent yet.
    """
    composites = [p for p in dac.all_profiles() if p.kind == "composite"]
    # Non-vacuity: a registry with no composite profile would satisfy the loop
    # below by never running it.
    assert composites, "expected at least one composite profile"
    for profile in composites:
        assert profile.final_edge_format != "S24_3LE", (
            f"{profile.id} declares S24_3LE, which outputd's paired composite "
            f"sink refuses at open (ChildPeriods::new -> EX_CONFIG 78 park): "
            f"the transport has no packed-24 child write path, so this ships a "
            f"speaker that cannot start"
        )


def test_a_composite_may_diverge_from_its_child_only_at_that_capability_gap() -> None:
    """The single/composite split model, asserted where it actually bites.

    A composite's declaration is what outputd asks BOTH children for: the
    reconciler emits ``JASPER_OUTPUTD_DAC_FORMAT`` from the ARMED profile's own
    id (``final_edge_format_for`` -> ``by_id``), so while a composite is armed no
    child profile's declaration is read, and while a single DAC is armed no
    composite's is. The two are therefore independent facts about two different
    armed configurations, not one fact stated twice.

    They may only diverge where the paired transport genuinely cannot follow the
    child — today, exactly the packed-24 gap. Any OTHER divergence is drift: two
    widths the same silicon could both run, differing for no reason anyone
    recorded. Asserting the live divergence case explicitly is what stops it
    rotting into an unexplained mismatch once the packed child path lands.
    """
    diverging: list[tuple[str, str, str, str]] = []
    for profile in dac.all_profiles():
        if profile.kind != "composite":
            continue
        for child_id in profile.child_profile_ids:
            child = dac.by_id(child_id)
            assert child is not None, f"{profile.id}: unknown child {child_id!r}"
            if child.final_edge_format != profile.final_edge_format:
                diverging.append(
                    (
                        profile.id,
                        profile.final_edge_format,
                        child.id,
                        child.final_edge_format,
                    )
                )

    for composite_id, composite_fmt, child_id, child_fmt in diverging:
        assert child_fmt == "S24_3LE" and composite_fmt != "S24_3LE", (
            f"{composite_id} declares {composite_fmt!r} but child {child_id} "
            f"declares {child_fmt!r}; the only divergence this registry permits "
            f"is a child on the packed S24_3LE edge that the paired composite "
            f"transport cannot drive"
        )

    # The live case, named — so removing it is a deliberate edit rather than a
    # loop that quietly stops finding anything to check.
    assert (
        DUAL_APPLE_USB_C_DAC_4CH.id,
        DUAL_APPLE_USB_C_DAC_4CH.final_edge_format,
        APPLE_USB_C_DONGLE.id,
        APPLE_USB_C_DONGLE.final_edge_format,
    ) in diverging, (
        "the dual-Apple composite and its Apple dongle child no longer diverge; "
        "if the paired sink gained a packed-24 child write path, move the "
        "composite to S24_3LE and delete this assertion in that same PR"
    )


def test_final_edge_format_rejects_unsupported_value() -> None:
    # The sentinel is `FLOAT_LE`, not the `S24_LE` this used to use. With the
    # packed `S24_3LE` now accepted, a 24-bit sentinel here reads as though the
    # accepted 24-bit width were being rejected — confusing at a glance and easy to
    # "fix" wrongly. `FLOAT_LE` is unambiguously outside the vocabulary (outputd's
    # is integer-only). `S24_LE` is still covered, in
    # test_the_accepted_final_edge_format_set_is_exactly_what_outputd_parses, where
    # the contrast with `S24_3LE` is the explicit subject.
    with pytest.raises(ValueError, match="unsupported final_edge_format"):
        DacProfile(
            id="bad_final_edge_format",
            label="Bad",
            kind="single",
            physical_output_count=2,
            coherent_clock_domain=True,
            clock_domain_label="Bad clock",
            clock_domain_contract="single_device",
            outputd_sink="alsa",
            supported_card_matches=("bad",),
            final_edge_format="FLOAT_LE",
        )


def test_final_edge_format_for_round_trips_for_bash() -> None:
    assert dac.final_edge_format_for(INNOMAKER_HIFI_AMP_PRO_ID) == "S32_LE"
    assert dac.final_edge_format_for(APPLE_USB_C_DONGLE_ID) == "S24_3LE"
    assert dac.final_edge_format_for("no_such_dac") is None
    # The lookup is BY ID, off whichever profile the reconciler armed — it never
    # walks into child_profile_ids. That is what keeps the Apple dongle's packed
    # S24_3LE edge out of a dual-Apple box, whose paired transport refuses it:
    # asking for the composite's id answers with the COMPOSITE's declaration
    # even though its two children are that very dongle profile.
    assert dac.final_edge_format_for(DUAL_APPLE_USB_C_DAC_4CH_ID) == "S16_LE"
    assert dac.by_id(DUAL_APPLE_USB_C_DAC_4CH_ID) is not None
    assert dac.by_id(DUAL_APPLE_USB_C_DAC_4CH_ID).child_profile_ids == (  # type: ignore[union-attr]
        APPLE_USB_C_DONGLE_ID,
        APPLE_USB_C_DONGLE_ID,
    )
    # Whatever it prints, `jasper-audio-hardware-reconcile` writes verbatim into
    # JASPER_OUTPUTD_DAC_FORMAT, so every reachable answer must be a value
    # outputd's config parser accepts — or the emit parks the final-output owner
    # at exit 78. Enumerated over the whole registry, not just the two named
    # above, so a new profile cannot introduce an unparseable value.
    for profile in dac.all_profiles():
        assert dac.final_edge_format_for(profile.id) in ("S16_LE", "S24_3LE", "S32_LE")


def test_every_registry_row_declares_a_sink_outputd_can_parse() -> None:
    """The reconciler writes ``outputd_sink`` verbatim into JASPER_OUTPUTD_SINK
    off the SAME probe as the edge format (ADR-0235 R1), so a spelling outputd's
    parser does not know parks the final-output owner at exit 78 exactly as an
    unknown format does. ``DacProfile.__post_init__`` only requires the field to
    be non-empty, so this is what stops a new row from introducing one. The Rust
    side of the vocabulary is pinned in tests/test_outputd_wiring.py.
    """

    for profile in dac.all_profiles():
        assert profile.outputd_sink in ("single_alsa", "dual_apple")
