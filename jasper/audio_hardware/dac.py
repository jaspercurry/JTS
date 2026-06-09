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
DUAL_APPLE_USB_C_DAC_4CH_ID = "dual_apple_usb_c_dac_4ch"

DAC8X_OUTPUTD_STABILITY_PROFILE = "hifiberry_dac8x_outputd_stability"

DacKind = Literal["single", "composite"]
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
    outputd_sink: str
    supported_card_matches: tuple[str, ...]
    usb_ids: tuple[str, ...] = ()
    child_profile_ids: tuple[str, ...] = ()
    requires_same_usb_bus: bool = False
    supports_active_outputd_lane: bool = False
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
    outputd_sink="alsa",
    supported_card_matches=("usb-c to 3.5mm",),
    usb_ids=("05ac:110a",),
    supports_active_outputd_lane=True,
    mixer_controls=(APPLE_HEADPHONE_CONTROL,),
    headphone_pinned_100=True,
    udev_rule="deploy/udev/99-jasper-apple-dongle.rules",
)

HIFIBERRY_DAC8X = DacProfile(
    id=HIFIBERRY_DAC8X_ID,
    label="HiFiBerry DAC8x / Studio DAC8x",
    kind="single",
    physical_output_count=8,
    coherent_clock_domain=True,
    clock_domain_label="Single HiFiBerry DAC8x device clock",
    outputd_sink="alsa",
    supported_card_matches=(
        "snd_rpi_hifiberry_dac8x",
        "hifiberry.*dac8x",
        "dac8x",
    ),
    supports_active_outputd_lane=True,
    validation_profile=DAC8X_OUTPUTD_STABILITY_PROFILE,
    dtoverlay="hifiberry-dac8x",
)

DUAL_APPLE_USB_C_DAC_4CH = DacProfile(
    id=DUAL_APPLE_USB_C_DAC_4CH_ID,
    label="Dual Apple USB-C audio adapters",
    kind="composite",
    physical_output_count=4,
    coherent_clock_domain=False,
    clock_domain_label="Dual Apple USB-C adapter independent clocks",
    outputd_sink="dual_apple",
    supported_card_matches=("usb-c to 3.5mm",),
    usb_ids=("05ac:110a",),
    child_profile_ids=(APPLE_USB_C_DONGLE_ID, APPLE_USB_C_DONGLE_ID),
    requires_same_usb_bus=True,
    supports_active_outputd_lane=True,
    headphone_pinned_100=True,
)


REGISTRY: tuple[DacProfile, ...] = (
    APPLE_USB_C_DONGLE,
    HIFIBERRY_DAC8X,
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


def supports_physical_output_count(profile_id: str, output_count: int) -> bool:
    """Return whether a known profile has exactly ``output_count`` outputs."""

    profile = by_id(profile_id)
    return profile is not None and profile.physical_output_count == output_count


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
    "DAC8X_OUTPUTD_STABILITY_PROFILE",
    "DUAL_APPLE_USB_C_DAC_4CH",
    "DUAL_APPLE_USB_C_DAC_4CH_ID",
    "DacKind",
    "DacProfile",
    "HIFIBERRY_DAC8X",
    "HIFIBERRY_DAC8X_ID",
    "MixerControl",
    "REGISTRY",
    "all_profiles",
    "by_id",
    "is_known_profile_id",
    "known_profile_ids",
    "mixer_control_groups_for",
    "physical_output_count_for",
    "supports_physical_output_count",
]
