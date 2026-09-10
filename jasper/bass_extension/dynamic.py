# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Measured, bounded inputs for CamillaDSP's native dynamic-bass block."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from numbers import Real
from typing import Any


MAX_DYNAMIC_BOOST_DB = 6.0
REFERENCE_LEVEL_DB_MIN = -100.0
REFERENCE_LEVEL_DB_MAX = 0.0
DETECTOR_CORNER_HZ_MIN = 20.0
DETECTOR_CORNER_HZ_MAX = 200.0
DELTA_HIGHPASS_HZ_MIN = 10.0
COMPRESSOR_THRESHOLD_DBFS_MIN = -60.0

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


def _finite(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a real number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
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
            for name in (
                "low_boost_db",
                "reference_level_db",
                "detector_lowpass_hz",
                "compressor_threshold_dbfs",
                "compressor_factor",
                "compressor_attack_s",
                "compressor_release_s",
            )
        }
        for name, number in values.items():
            object.__setattr__(self, name, number)
        if not 0.0 < values["low_boost_db"] <= MAX_DYNAMIC_BOOST_DB:
            raise ValueError(f"low_boost_db must be in (0, {MAX_DYNAMIC_BOOST_DB}]")
        if not REFERENCE_LEVEL_DB_MIN <= values["reference_level_db"] <= REFERENCE_LEVEL_DB_MAX:
            raise ValueError("reference_level_db is outside the Main-fader domain")
        if not DETECTOR_CORNER_HZ_MIN <= values["detector_lowpass_hz"] <= DETECTOR_CORNER_HZ_MAX:
            raise ValueError("detector_lowpass_hz is outside the measured bass domain")
        if not COMPRESSOR_THRESHOLD_DBFS_MIN <= values["compressor_threshold_dbfs"] <= 0.0:
            raise ValueError("compressor_threshold_dbfs must be in [-60, 0]")
        if not 1.0 < values["compressor_factor"] <= 20.0:
            raise ValueError("compressor_factor must be in (1, 20]")
        if not 0.001 <= values["compressor_attack_s"] <= 0.1:
            raise ValueError("compressor_attack_s must be in [0.001, 0.1]")
        if not 0.01 <= values["compressor_release_s"] <= 2.0:
            raise ValueError("compressor_release_s must be in [0.01, 2]")
        if self.delta_highpass_hz is not None:
            corner = _finite(self.delta_highpass_hz, "delta_highpass_hz")
            if not DELTA_HIGHPASS_HZ_MIN <= corner < values["detector_lowpass_hz"]:
                raise ValueError(
                    "delta_highpass_hz must be in the measured band below detector_lowpass_hz"
                )
            object.__setattr__(self, "delta_highpass_hz", corner)


def validate_dynamic_bass_descriptor(value: Any) -> dict[str, Any]:
    """Return the normalized strict candidate payload or raise ``ValueError``."""

    if not isinstance(value, Mapping):
        raise ValueError("dynamic_bass must be an object")
    keys = set(value)
    if not _REQUIRED_FIELDS <= keys or not keys <= _REQUIRED_FIELDS | _OPTIONAL_FIELDS:
        raise ValueError("dynamic_bass has unknown or missing fields")
    descriptor = DynamicBassDescriptor(**dict(value))
    return {
        "low_boost_db": descriptor.low_boost_db,
        "reference_level_db": descriptor.reference_level_db,
        "detector_lowpass_hz": descriptor.detector_lowpass_hz,
        "compressor_threshold_dbfs": descriptor.compressor_threshold_dbfs,
        "compressor_factor": descriptor.compressor_factor,
        "compressor_attack_s": descriptor.compressor_attack_s,
        "compressor_release_s": descriptor.compressor_release_s,
        "delta_highpass_hz": descriptor.delta_highpass_hz,
    }


def loudness_boost_db(canonical_volume_db: float, descriptor: DynamicBassDescriptor) -> float:
    """CamillaDSP v4.1.3's exact 20 dB Loudness interpolation law."""

    level = _finite(canonical_volume_db, "canonical_volume_db")
    fraction = max(0.0, min(1.0, (descriptor.reference_level_db - level) / 20.0))
    return descriptor.low_boost_db * fraction


def maximum_output_gain_db(
    main_volume_db: float, descriptor: DynamicBassDescriptor
) -> float:
    """Bound output when Main and the canonical Aux loudness level are equal."""

    level = _finite(main_volume_db, "main_volume_db")
    return level + loudness_boost_db(level, descriptor)
