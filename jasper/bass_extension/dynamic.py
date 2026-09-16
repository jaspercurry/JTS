# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Measured, bounded inputs for CamillaDSP's native dynamic-bass block."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields
from typing import Any

from jasper.camilla_config_contract import SHELF_Q, FilterSpec
from jasper.json_fields import finite_float
from jasper.sound.profile import _filter_response_complex, _freq_trig


# CamillaDSP v4.1.3 Loudness parameter range; not a driver capability estimate.
# https://github.com/HEnquist/camilladsp/blob/v4.1.3/README.md#loudness
NATIVE_LOUDNESS_BOOST_MAX_DB = 20.0
# CamillaDSP's native Loudness low-shelf corner used by the proof model.
NATIVE_LOUDNESS_CORNER_HZ = 70.0
LOUDNESS_TAPER_DB = 20.0
REFERENCE_LEVEL_DB_MIN = -100.0
REFERENCE_LEVEL_DB_MAX = 0.0
DETECTOR_CORNER_HZ_MIN = 20.0
DETECTOR_CORNER_HZ_MAX = 200.0
DELTA_HIGHPASS_HZ_MIN = 10.0
COMPRESSOR_THRESHOLD_DBFS_MIN = -60.0
COMPRESSOR_THRESHOLD_DBFS_MAX = 0.0
LOW_BOOST_DB_MIN = 0.0
COMPRESSOR_FACTOR_MIN = 1.0
COMPRESSOR_FACTOR_MAX = 20.0
COMPRESSOR_ATTACK_S_MIN = 0.001
COMPRESSOR_ATTACK_S_MAX = 0.1
COMPRESSOR_RELEASE_S_MIN = 0.01
COMPRESSOR_RELEASE_S_MAX = 2.0

_REQUIRED_FIELDS = {
    "low_boost_db",
    "reference_level_db",
    "detector_lowpass_hz",
    "compressor_threshold_dbfs",
}
_OPTIONAL_FIELDS = {
    "compressor_factor",
    "compressor_attack_s",
    "compressor_release_s",
    "delta_highpass_hz",
}

DYNAMIC_BASS_REFUSAL_REASONS = frozenset({"bass_descriptor_malformed"} | {
    f"bass_{name}_invalid" for name in _REQUIRED_FIELDS | _OPTIONAL_FIELDS
})


class DynamicBassDescriptorError(ValueError):
    def __init__(self, field: str, detail: str) -> None:
        super().__init__(detail)
        self.field = field
        self.reason = "bass_descriptor_malformed" if field == "dynamic_bass" else f"bass_{field}_invalid"


def _finite(value: float, name: str) -> float:
    number = finite_float(value)
    if number is None:
        raise DynamicBassDescriptorError(name, f"{name} must be a finite real number")
    return number


@dataclass(frozen=True)
class DynamicBassDescriptor:
    """The one measured setting used by the native runtime graph."""

    low_boost_db: float
    reference_level_db: float
    detector_lowpass_hz: float
    compressor_threshold_dbfs: float
    compressor_factor: float = 10.0
    compressor_attack_s: float = 0.01
    compressor_release_s: float = 0.25
    delta_highpass_hz: float | None = None

    def __post_init__(self) -> None:
        values = {
            name: _finite(getattr(self, name), name)
            for name in (field.name for field in fields(self) if field.name != "delta_highpass_hz")
        }
        for name, number in values.items():
            object.__setattr__(self, name, number)
        if not LOW_BOOST_DB_MIN < values["low_boost_db"] <= NATIVE_LOUDNESS_BOOST_MAX_DB:
            raise DynamicBassDescriptorError("low_boost_db", f"low_boost_db must be in (0, {NATIVE_LOUDNESS_BOOST_MAX_DB}]")
        if not REFERENCE_LEVEL_DB_MIN <= values["reference_level_db"] <= REFERENCE_LEVEL_DB_MAX:
            raise DynamicBassDescriptorError("reference_level_db", "reference_level_db is outside the Main-fader domain")
        if not DETECTOR_CORNER_HZ_MIN <= values["detector_lowpass_hz"] <= DETECTOR_CORNER_HZ_MAX:
            raise DynamicBassDescriptorError("detector_lowpass_hz", "detector_lowpass_hz is outside the measured bass domain")
        if not COMPRESSOR_THRESHOLD_DBFS_MIN <= values["compressor_threshold_dbfs"] <= COMPRESSOR_THRESHOLD_DBFS_MAX:
            raise DynamicBassDescriptorError("compressor_threshold_dbfs", "compressor_threshold_dbfs must be in [-60, 0]")
        if not COMPRESSOR_FACTOR_MIN < values["compressor_factor"] <= COMPRESSOR_FACTOR_MAX:
            raise DynamicBassDescriptorError("compressor_factor", "compressor_factor must be in (1, 20]")
        if not COMPRESSOR_ATTACK_S_MIN <= values["compressor_attack_s"] <= COMPRESSOR_ATTACK_S_MAX:
            raise DynamicBassDescriptorError("compressor_attack_s", "compressor_attack_s must be in [0.001, 0.1]")
        if not COMPRESSOR_RELEASE_S_MIN <= values["compressor_release_s"] <= COMPRESSOR_RELEASE_S_MAX:
            raise DynamicBassDescriptorError("compressor_release_s", "compressor_release_s must be in [0.01, 2]")
        if self.delta_highpass_hz is not None:
            corner = _finite(self.delta_highpass_hz, "delta_highpass_hz")
            if not DELTA_HIGHPASS_HZ_MIN <= corner < values["detector_lowpass_hz"]:
                raise DynamicBassDescriptorError("delta_highpass_hz",
                    "delta_highpass_hz must be in the measured band below detector_lowpass_hz"
                )
            object.__setattr__(self, "delta_highpass_hz", corner)


def validate_dynamic_bass_descriptor(value: Any) -> dict[str, Any]:
    """Return the normalized strict candidate payload or raise ``ValueError``."""

    if not isinstance(value, Mapping):
        raise DynamicBassDescriptorError("dynamic_bass", "dynamic_bass must be an object")
    keys = set(value)
    if not _REQUIRED_FIELDS <= keys or not keys <= _REQUIRED_FIELDS | _OPTIONAL_FIELDS:
        raise DynamicBassDescriptorError("dynamic_bass", "dynamic_bass has unknown or missing fields")
    descriptor = DynamicBassDescriptor(**dict(value))
    return {name: getattr(descriptor, name) for name in sorted(_REQUIRED_FIELDS | _OPTIONAL_FIELDS)}


def loudness_boost_db(canonical_volume_db: float, descriptor: DynamicBassDescriptor) -> float:
    """CamillaDSP v4.1.3's exact 20 dB Loudness interpolation law."""

    level = _finite(canonical_volume_db, "canonical_volume_db")
    fraction = max(0.0, min(1.0, (descriptor.reference_level_db - level) / LOUDNESS_TAPER_DB))
    return descriptor.low_boost_db * fraction


def expected_boost_db(
    descriptor: DynamicBassDescriptor, fader_db: float, freqs_hz: Iterable[float],
) -> list[float]:
    """Model CamillaDSP's uncompressed slope-12 shelf with a delta high-pass."""
    frequencies = list(freqs_hz)
    trig = _freq_trig(frequencies)
    shelf = _filter_response_complex(
        FilterSpec("native_low", "Lowshelf", NATIVE_LOUDNESS_CORNER_HZ, loudness_boost_db(fader_db, descriptor)), frequencies, trig,
    )
    highpass = _filter_response_complex(
        FilterSpec("delta_highpass", "Highpass", descriptor.delta_highpass_hz, 0.0, SHELF_Q), frequencies, trig,
    ) if descriptor.delta_highpass_hz is not None else [1.0] * len(frequencies)
    return [20.0 * math.log10(abs(1.0 + (low - 1.0) * hp)) for low, hp in zip(shelf, highpass)]


def dynamic_bass_gain_reserve_db(descriptor: DynamicBassDescriptor) -> float:
    """Bound the static filter/delta gain; the final limiter owns sample peaks."""
    # Native slope-12 shelf: |H-1| <= sqrt((2+sqrt(5))/4) * (10**(B/20)-1).
    # A Butterworth delta high-pass and gain-only compression cannot enlarge its L2 norm.
    delta_ratio = math.sqrt((2.0 + math.sqrt(5.0)) / 4.0)
    delta_gain = 10.0 ** (descriptor.low_boost_db / 20.0) - 1.0
    return 20.0 * math.log10(1.0 + delta_ratio * delta_gain)
