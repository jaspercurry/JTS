"""Preference-EQ and sound-curve substrate."""

from .profile import (
    CURVE_PRESETS,
    ParametricBand,
    SimpleEq,
    SoundProfile,
    build_sound_filters,
    estimate_compare_headroom_db,
    estimate_headroom_db,
    response_component_payload,
    response_preview,
)

__all__ = [
    "CURVE_PRESETS",
    "ParametricBand",
    "SimpleEq",
    "SoundProfile",
    "build_sound_filters",
    "estimate_compare_headroom_db",
    "estimate_headroom_db",
    "response_component_payload",
    "response_preview",
]
