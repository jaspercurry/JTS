"""Audio hardware profile registries."""
from __future__ import annotations

from .dac import (
    APPLE_HEADPHONE_CONTROL,
    APPLE_USB_C_DONGLE,
    APPLE_USB_C_DONGLE_ID,
    DAC8X_OUTPUTD_STABILITY_PROFILE,
    DUAL_APPLE_USB_C_DAC_4CH,
    DUAL_APPLE_USB_C_DAC_4CH_ID,
    DacKind,
    DacProfile,
    HIFIBERRY_DAC8X,
    HIFIBERRY_DAC8X_ID,
    MixerControl,
    REGISTRY,
    all_profiles,
    by_id,
    is_known_profile_id,
    known_profile_ids,
    physical_output_count_for,
    supports_physical_output_count,
)

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
    "physical_output_count_for",
    "supports_physical_output_count",
]
