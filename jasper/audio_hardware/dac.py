# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Static DAC profile registry for JTS output hardware.

This module is deliberately IO-free. It describes known output hardware
capabilities and quirks; it does not probe ALSA, read env files, render
system config, or restart services. Runtime ownership stays with
``jasper.output_topology``, ``jasper.output_hardware`` once landed,
``jasper-audio-hardware-reconcile``, and ``jasper-outputd``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


APPLE_USB_C_DONGLE_ID = "apple_usb_c_dongle"
HIFIBERRY_DAC8X_ID = "hifiberry_dac8x"
HIFIBERRY_DAC8X_STUDIO_ID = "hifiberry_dac8x_studio"
DUAL_APPLE_USB_C_DAC_4CH_ID = "dual_apple_usb_c_dac_4ch"

DAC8X_OUTPUTD_STABILITY_PROFILE = "hifiberry_dac8x_outputd_stability"

DacKind = Literal["single", "composite"]
ClockDomainContract = Literal[
    "single_device",
    "independent",
    "measured_sync_required",
]
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")


@dataclass(frozen=True)
class MixerControl:
    """A mixer control policy a runtime owner may enforce.

    The registry only declares intent. Scripts such as
    ``jasper-dac-init`` and ``jasper-headphone-monitor`` remain the
    components that actually apply or monitor mixer state.
    """

    name: str
    target_percent: int | None = None
    unmute: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("mixer control name is required")
        if self.target_percent is not None and not 0 <= self.target_percent <= 100:
            raise ValueError("mixer target_percent must be 0..100")


@dataclass(frozen=True)
class ChannelMapEntry:
    """One CamillaDSP-output → physical-DAC-channel routing hop for the active lane.

    Pure routing: "CamillaDSP active-output channel ``camilla_out_index`` drives
    physical DAC channel ``physical_dac_channel``." It carries **no gain** —
    CamillaDSP owns the gain stage — so a `dac_channel_map` is a permutation, not
    a mixer. This keeps lane→pin assignment as declarative data the transport
    reads rather than a per-DAC code branch.
    """

    camilla_out_index: int
    physical_dac_channel: int

    def __post_init__(self) -> None:
        if self.camilla_out_index < 0:
            raise ValueError("camilla_out_index must be >= 0")
        if self.physical_dac_channel < 0:
            raise ValueError("physical_dac_channel must be >= 0")


@dataclass(frozen=True)
class DacProfile:
    """One supported final-output DAC shape.

    ``supported_card_matches`` are case-insensitive regex fragments used
    by detector/reconciler code to recognize ALSA card listings. They
    are data hints, not active probes.
    """

    id: str
    label: str
    kind: DacKind
    physical_output_count: int
    coherent_clock_domain: bool
    clock_domain_label: str
    clock_domain_contract: ClockDomainContract
    outputd_sink: str
    supported_card_matches: tuple[str, ...]
    usb_ids: tuple[str, ...] = ()
    child_profile_ids: tuple[str, ...] = ()
    requires_same_usb_bus: bool = False
    supports_active_outputd_lane: bool = False
    active_outputd_lane_channels: int | None = None
    dac_channel_map: tuple[ChannelMapEntry, ...] | None = None
    mixer_controls: tuple[MixerControl, ...] = ()
    headphone_pinned_100: bool = False
    validation_profile: str | None = None
    udev_rule: str | None = None
    dtoverlay: str | None = None

    def __post_init__(self) -> None:
        if not _ID_RE.match(self.id):
            raise ValueError(f"unsupported DAC profile id: {self.id!r}")
        if not self.label.strip():
            raise ValueError(f"{self.id}: label is required")
        if self.kind not in ("single", "composite"):
            raise ValueError(f"{self.id}: unsupported kind {self.kind!r}")
        if self.physical_output_count < 0:
            raise ValueError(f"{self.id}: physical_output_count must be >= 0")
        if not self.clock_domain_label.strip():
            raise ValueError(f"{self.id}: clock_domain_label is required")
        if self.clock_domain_contract not in (
            "single_device",
            "independent",
            "measured_sync_required",
        ):
            raise ValueError(
                f"{self.id}: unsupported clock_domain_contract "
                f"{self.clock_domain_contract!r}"
            )
        if not self.outputd_sink.strip():
            raise ValueError(f"{self.id}: outputd_sink is required")
        if not self.supported_card_matches and not self.child_profile_ids:
            raise ValueError(
                f"{self.id}: supported_card_matches or child_profile_ids required"
            )
        for pattern in self.supported_card_matches:
            re.compile(pattern, re.IGNORECASE)
        if self.kind == "single" and self.child_profile_ids:
            raise ValueError(f"{self.id}: single DAC profile cannot have children")
        if self.kind == "composite" and len(self.child_profile_ids) < 2:
            raise ValueError(f"{self.id}: composite DAC profile needs children")
        if self.kind == "composite" and self.mixer_controls:
            raise ValueError(
                f"{self.id}: composite mixer controls must come from children"
            )
        if self.requires_same_usb_bus and self.kind != "composite":
            raise ValueError(f"{self.id}: same-bus requirement only fits composites")
        if self.kind == "single" and self.clock_domain_contract != "single_device":
            raise ValueError(
                f"{self.id}: single DAC profiles use single_device clock contract"
            )
        if self.kind == "composite" and self.clock_domain_contract == "single_device":
            raise ValueError(
                f"{self.id}: composite DAC profile cannot use single_device "
                "clock contract"
            )
        if self.coherent_clock_domain and self.clock_domain_contract != "single_device":
            raise ValueError(
                f"{self.id}: coherent_clock_domain only describes single-device "
                "clock domains"
            )
        if self.supports_active_outputd_lane:
            if self.active_outputd_lane_channels is None:
                raise ValueError(
                    f"{self.id}: active_outputd_lane_channels is required when "
                    "supports_active_outputd_lane is true"
                )
            if self.active_outputd_lane_channels <= 0:
                raise ValueError(
                    f"{self.id}: active_outputd_lane_channels must be > 0"
                )
            if self.active_outputd_lane_channels > self.physical_output_count:
                raise ValueError(
                    f"{self.id}: active_outputd_lane_channels cannot exceed "
                    "physical_output_count"
                )
        elif self.active_outputd_lane_channels is not None:
            raise ValueError(
                f"{self.id}: active_outputd_lane_channels requires "
                "supports_active_outputd_lane"
            )
        if self.dac_channel_map is not None:
            # The channel map routes the active lane; it only means something
            # for a DAC that has one. Validate it is a clean permutation of the
            # transport width onto distinct, in-range physical channels — a
            # malformed map is fail-closed at import, before any deploy.
            if not self.supports_active_outputd_lane:
                raise ValueError(
                    f"{self.id}: dac_channel_map requires supports_active_outputd_lane"
                )
            width = self.active_outputd_lane_channels
            if len(self.dac_channel_map) != width:
                raise ValueError(
                    f"{self.id}: dac_channel_map needs one entry per active-lane "
                    f"channel ({width}), got {len(self.dac_channel_map)}"
                )
            camilla_indexes = sorted(e.camilla_out_index for e in self.dac_channel_map)
            if camilla_indexes != list(range(width)):
                raise ValueError(
                    f"{self.id}: dac_channel_map camilla_out_index values must be "
                    f"exactly 0..{width - 1} with no gaps or duplicates"
                )
            physical = [e.physical_dac_channel for e in self.dac_channel_map]
            if len(set(physical)) != len(physical):
                raise ValueError(
                    f"{self.id}: dac_channel_map maps two lanes to the same "
                    "physical_dac_channel"
                )
            for channel in physical:
                if channel >= self.physical_output_count:
                    raise ValueError(
                        f"{self.id}: dac_channel_map physical_dac_channel {channel} "
                        f"exceeds physical_output_count {self.physical_output_count}"
                    )

    def is_coherent_single(self) -> bool:
        """True when this is one device on a single coherent clock domain.

        The shape that takes the simple single-PCM transport: one ALSA device,
        one clock, no inter-device drift correction. Folds the
        ``kind == "single" and coherent_clock_domain`` check that active-route
        resolution would otherwise inline.
        """

        return self.kind == "single" and self.coherent_clock_domain


APPLE_HEADPHONE_CONTROL = MixerControl(
    name="Headphone",
    target_percent=100,
    unmute=True,
)

APPLE_USB_C_DONGLE = DacProfile(
    id=APPLE_USB_C_DONGLE_ID,
    label="Apple USB-C audio adapter",
    kind="single",
    physical_output_count=2,
    coherent_clock_domain=True,
    clock_domain_label="Single Apple USB audio device clock",
    clock_domain_contract="single_device",
    outputd_sink="alsa",
    supported_card_matches=("usb-c to 3.5mm",),
    usb_ids=("05ac:110a",),
    mixer_controls=(APPLE_HEADPHONE_CONTROL,),
    headphone_pinned_100=True,
    # A single Apple dongle can carry a mono active 2-way graph over the same
    # width-aware single-ALSA active lane used by wider coherent DACs.
    supports_active_outputd_lane=True,
    active_outputd_lane_channels=2,
    udev_rule="deploy/udev/99-jasper-apple-dongle.rules",
)

HIFIBERRY_DAC8X = DacProfile(
    id=HIFIBERRY_DAC8X_ID,
    label="HiFiBerry DAC8x",
    kind="single",
    physical_output_count=8,
    coherent_clock_domain=True,
    clock_domain_label="Single HiFiBerry DAC8x device clock",
    clock_domain_contract="single_device",
    outputd_sink="alsa",
    supported_card_matches=(
        "snd_rpi_hifiberry_dac8x(?!.*studio)",
        "hifiberry.*dac8x(?!.*studio)",
        r"\bdac8x\b(?!.*studio)",
    ),
    # The DAC-agnostic active-output transport (Stage 1) can now carry a
    # coherent single DAC of any width, so the 8-channel DAC8x rides the
    # active-crossover lane end-to-end. The transport builds an identity
    # channel map when dac_channel_map is None (one coherent clock domain,
    # no permutation needed). Width is DATA, not a per-DAC code branch.
    supports_active_outputd_lane=True,
    active_outputd_lane_channels=8,
    validation_profile=DAC8X_OUTPUTD_STABILITY_PROFILE,
    dtoverlay="hifiberry-dac8x",
)

HIFIBERRY_DAC8X_STUDIO = DacProfile(
    id=HIFIBERRY_DAC8X_STUDIO_ID,
    label="HiFiBerry DAC8x Studio",
    kind="single",
    physical_output_count=8,
    coherent_clock_domain=True,
    clock_domain_label="Single HiFiBerry DAC8x Studio device clock",
    clock_domain_contract="single_device",
    outputd_sink="alsa",
    supported_card_matches=(
        "dac8x.*studio",
        "hifiberry.*dac8x.*studio",
    ),
    # Same active-lane shape as the base DAC8x: a coherent 8-channel single
    # device on the DAC-agnostic transport (Stage 1). dac_channel_map None =>
    # identity map.
    supports_active_outputd_lane=True,
    active_outputd_lane_channels=8,
    validation_profile=DAC8X_OUTPUTD_STABILITY_PROFILE,
    dtoverlay="hifiberry-dac8x",
)

DUAL_APPLE_USB_C_DAC_4CH = DacProfile(
    id=DUAL_APPLE_USB_C_DAC_4CH_ID,
    label="Dual Apple USB-C DAC 4-channel pair",
    kind="composite",
    physical_output_count=4,
    coherent_clock_domain=False,
    clock_domain_label="Dual Apple USB-C DAC pair (measured sync required)",
    clock_domain_contract="measured_sync_required",
    outputd_sink="dual_apple",
    supported_card_matches=("usb-c to 3.5mm",),
    usb_ids=("05ac:110a",),
    child_profile_ids=(APPLE_USB_C_DONGLE_ID, APPLE_USB_C_DONGLE_ID),
    requires_same_usb_bus=True,
    supports_active_outputd_lane=True,
    active_outputd_lane_channels=4,
    headphone_pinned_100=True,
)


REGISTRY: tuple[DacProfile, ...] = (
    APPLE_USB_C_DONGLE,
    HIFIBERRY_DAC8X,
    HIFIBERRY_DAC8X_STUDIO,
    DUAL_APPLE_USB_C_DAC_4CH,
)


def _build_index(profiles: tuple[DacProfile, ...]) -> dict[str, DacProfile]:
    out: dict[str, DacProfile] = {}
    for profile in profiles:
        if profile.id in out:
            raise ValueError(f"duplicate DAC profile id: {profile.id}")
        out[profile.id] = profile
    for profile in profiles:
        for child_id in profile.child_profile_ids:
            if child_id not in out:
                raise ValueError(
                    f"{profile.id}: unknown child DAC profile id {child_id!r}"
                )
    return out


_BY_ID = _build_index(REGISTRY)


def all_profiles() -> tuple[DacProfile, ...]:
    """Return all known DAC profiles in stable display order."""

    return REGISTRY


def by_id(profile_id: str) -> DacProfile | None:
    """Lookup a DAC profile by stable id."""

    return _BY_ID.get(profile_id)


def known_profile_ids() -> tuple[str, ...]:
    """Return known DAC profile ids in stable display order."""

    return tuple(profile.id for profile in REGISTRY)


def is_known_profile_id(profile_id: str) -> bool:
    """Return True when ``profile_id`` is a registered DAC profile."""

    return profile_id in _BY_ID


def physical_output_count_for(profile_id: str) -> int | None:
    """Return the declared physical output count for a known profile."""

    profile = by_id(profile_id)
    if profile is None:
        return None
    return profile.physical_output_count


def label_for(profile_id: str) -> str | None:
    """Return the display label for a known profile."""

    profile = by_id(profile_id)
    if profile is None:
        return None
    return profile.label


def clock_domain_label_for(profile_id: str) -> str | None:
    """Return the clock-domain label for a known profile."""

    profile = by_id(profile_id)
    if profile is None:
        return None
    return profile.clock_domain_label


def clock_domain_contract_for(profile_id: str) -> ClockDomainContract | None:
    """Return the clock-domain contract for a known profile."""

    profile = by_id(profile_id)
    if profile is None:
        return None
    return profile.clock_domain_contract


def profile_for_card_label(label: str) -> DacProfile | None:
    """Return the first single-device profile matching an ALSA/sysfs label."""

    text = label.strip()
    if not text:
        return None
    for profile in REGISTRY:
        if profile.kind != "single":
            continue
        if any(
            re.search(pattern, text, re.IGNORECASE)
            for pattern in profile.supported_card_matches
        ):
            return profile
    return None


def supports_physical_output_count(profile_id: str, output_count: int) -> bool:
    """Return whether a known profile has exactly ``output_count`` outputs."""

    profile = by_id(profile_id)
    return profile is not None and profile.physical_output_count == output_count


def active_outputd_lane_channels_for(profile_id: str) -> int | None:
    """Return the profile-declared active outputd transport width.

    This is the protected transport capacity between CamillaDSP and outputd for
    the current implementation. It is deliberately separate from physical DAC
    outputs: a DAC can expose more analog lanes than outputd can safely consume
    through the active-speaker handoff today.
    """

    profile = by_id(profile_id)
    if profile is None or not profile.supports_active_outputd_lane:
        return None
    return profile.active_outputd_lane_channels


def mixer_control_groups_for(
    profile_id: str,
) -> tuple[tuple[MixerControl, ...], ...] | None:
    """Return mixer policies grouped by physical DAC child.

    A single DAC returns one group. A composite profile returns one
    group for each child profile, preserving cardinality for callers
    that need to pin or monitor child-device controls.
    """

    profile = by_id(profile_id)
    if profile is None:
        return None
    if profile.kind == "single":
        return (profile.mixer_controls,)
    groups: list[tuple[MixerControl, ...]] = []
    for child_id in profile.child_profile_ids:
        child = by_id(child_id)
        if child is None:
            return None
        groups.append(child.mixer_controls)
    return tuple(groups)


__all__ = [
    "APPLE_HEADPHONE_CONTROL",
    "APPLE_USB_C_DONGLE",
    "APPLE_USB_C_DONGLE_ID",
    "ChannelMapEntry",
    "ClockDomainContract",
    "DAC8X_OUTPUTD_STABILITY_PROFILE",
    "DUAL_APPLE_USB_C_DAC_4CH",
    "DUAL_APPLE_USB_C_DAC_4CH_ID",
    "DacKind",
    "DacProfile",
    "HIFIBERRY_DAC8X",
    "HIFIBERRY_DAC8X_ID",
    "HIFIBERRY_DAC8X_STUDIO",
    "HIFIBERRY_DAC8X_STUDIO_ID",
    "MixerControl",
    "REGISTRY",
    "all_profiles",
    "active_outputd_lane_channels_for",
    "by_id",
    "clock_domain_contract_for",
    "clock_domain_label_for",
    "is_known_profile_id",
    "known_profile_ids",
    "label_for",
    "mixer_control_groups_for",
    "profile_for_card_label",
    "physical_output_count_for",
    "supports_physical_output_count",
]
