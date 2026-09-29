# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Measured, bounded inputs for CamillaDSP's native dynamic-bass block."""

from __future__ import annotations

import functools
import math
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, fields
from typing import Any

from jasper.platform.biquad import (
    RESPONSE_SAMPLE_RATE_HZ, SHELF_Q, FilterSpec, biquad_response_complex, filter_response_complex, freq_trig,
)
from jasper.platform.json_fields import finite_float


DETECTOR_CORNER_HZ_MIN = 20.0
DETECTOR_CORNER_HZ_MAX = 200.0
DELTA_HIGHPASS_HZ_MIN = 10.0
COMPRESSOR_THRESHOLD_DBFS_MIN = -60.0
COMPRESSOR_THRESHOLD_DBFS_MAX = 0.0
COMPRESSOR_FACTOR_MIN = 1.0
COMPRESSOR_FACTOR_MAX = 20.0
COMPRESSOR_ATTACK_S_MIN = 0.001
COMPRESSOR_ATTACK_S_MAX = 0.1
COMPRESSOR_RELEASE_S_MIN = 0.01
COMPRESSOR_RELEASE_S_MAX = 2.0
LINKWITZ_SOURCE_HZ_MIN = DETECTOR_CORNER_HZ_MIN
LINKWITZ_SOURCE_HZ_MAX = DETECTOR_CORNER_HZ_MAX
LINKWITZ_TARGET_HZ_MIN = DELTA_HIGHPASS_HZ_MIN
LINKWITZ_Q_MIN = 0.3
LINKWITZ_Q_MAX = 1.5
# A 1/48-octave grid under-reads a Q <= 1.5 delta peak by < 0.003 dB (ADR-0352).
_RESERVE_GRID_MARGIN_DB = 0.01

_DETECTOR_FIELDS = {"detector_lowpass_hz", "compressor_threshold_dbfs"}
REQUIRED_FIELDS = _DETECTOR_FIELDS | {"linkwitz_transform", "delta_highpass_hz"}
OPTIONAL_FIELDS = {"compressor_factor", "compressor_attack_s", "compressor_release_s"}
_FIELDS = REQUIRED_FIELDS | OPTIONAL_FIELDS
# A stored section that still carries one of these needs a tune in the ADR-0359 form (ADR-0381).
_ADR_0352_FIELDS = {"low_boost_db", "reference_level_db"}

DYNAMIC_BASS_REFUSAL_REASONS = frozenset({"bass_descriptor_malformed"} | {f"bass_{name}_invalid" for name in _FIELDS})


class DynamicBassDescriptorError(ValueError):
    def __init__(self, field: str, detail: str) -> None:
        super().__init__(detail)
        self.field = field
        self.reason = f"bass_{field}_invalid" if field in _FIELDS else "bass_descriptor_malformed"


def _finite(value: object, name: str) -> float:
    number = finite_float(value)
    if number is None:
        raise DynamicBassDescriptorError(name, f"{name} must be a finite real number")
    return number


@dataclass(frozen=True)
class LinkwitzTransform:
    """The boost: the woofer's alignment (source) moved to the target (ADR-0352, ADR-0359)."""

    source_hz: float
    source_q: float
    target_hz: float
    target_q: float

    def __post_init__(self) -> None:
        for field in fields(self):
            object.__setattr__(self, field.name, _finite(getattr(self, field.name), "linkwitz_transform"))
        if not (LINKWITZ_SOURCE_HZ_MIN <= self.source_hz <= LINKWITZ_SOURCE_HZ_MAX
                and LINKWITZ_TARGET_HZ_MIN <= self.target_hz < self.source_hz
                and LINKWITZ_Q_MIN <= min(self.source_q, self.target_q)
                and max(self.source_q, self.target_q) <= LINKWITZ_Q_MAX):
            raise DynamicBassDescriptorError("linkwitz_transform", "linkwitz_transform is outside its bounds")


@dataclass(frozen=True)
class DynamicBassDescriptor:
    """The one measured setting used by the native runtime graph."""

    detector_lowpass_hz: float
    compressor_threshold_dbfs: float
    linkwitz_transform: LinkwitzTransform
    delta_highpass_hz: float
    compressor_factor: float = 10.0
    compressor_attack_s: float = 0.01
    compressor_release_s: float = 0.25

    def __post_init__(self) -> None:
        for name in sorted(_DETECTOR_FIELDS | OPTIONAL_FIELDS):
            object.__setattr__(self, name, _finite(getattr(self, name), name))
        if not DETECTOR_CORNER_HZ_MIN <= self.detector_lowpass_hz <= DETECTOR_CORNER_HZ_MAX:
            raise DynamicBassDescriptorError("detector_lowpass_hz", "detector_lowpass_hz is outside the measured bass domain")
        if not COMPRESSOR_THRESHOLD_DBFS_MIN <= self.compressor_threshold_dbfs <= COMPRESSOR_THRESHOLD_DBFS_MAX:
            raise DynamicBassDescriptorError("compressor_threshold_dbfs", "compressor_threshold_dbfs must be in [-60, 0]")
        if not COMPRESSOR_FACTOR_MIN < self.compressor_factor <= COMPRESSOR_FACTOR_MAX:
            raise DynamicBassDescriptorError("compressor_factor", "compressor_factor must be in (1, 20]")
        if not COMPRESSOR_ATTACK_S_MIN <= self.compressor_attack_s <= COMPRESSOR_ATTACK_S_MAX:
            raise DynamicBassDescriptorError("compressor_attack_s", "compressor_attack_s must be in [0.001, 0.1]")
        if not COMPRESSOR_RELEASE_S_MIN <= self.compressor_release_s <= COMPRESSOR_RELEASE_S_MAX:
            raise DynamicBassDescriptorError("compressor_release_s", "compressor_release_s must be in [0.01, 2]")
        corner = _finite(self.delta_highpass_hz, "delta_highpass_hz")
        if not DELTA_HIGHPASS_HZ_MIN <= corner < self.detector_lowpass_hz:
            raise DynamicBassDescriptorError("delta_highpass_hz",
                "delta_highpass_hz must be in the measured band below detector_lowpass_hz"
            )
        object.__setattr__(self, "delta_highpass_hz", corner)
        shape = self.linkwitz_transform
        if isinstance(shape, Mapping):
            if set(shape) != {field.name for field in fields(LinkwitzTransform)}:
                raise DynamicBassDescriptorError("linkwitz_transform",
                    "linkwitz_transform needs exactly source_hz, source_q, target_hz and target_q")
            shape = LinkwitzTransform(**shape)
        elif not isinstance(shape, LinkwitzTransform):
            raise DynamicBassDescriptorError("linkwitz_transform", "linkwitz_transform must be an object")
        object.__setattr__(self, "linkwitz_transform", shape)

    def payload(self) -> dict[str, Any]:
        """The normalized candidate payload."""
        payload = {name: getattr(self, name) for name in sorted(_FIELDS)}
        payload["linkwitz_transform"] = asdict(self.linkwitz_transform)
        return payload


def validate_dynamic_bass_descriptor(value: Any) -> dict[str, Any]:
    """Return the normalized strict candidate payload or raise ``ValueError``; an unknown field refuses by name (#2902)."""

    if not isinstance(value, Mapping):
        raise DynamicBassDescriptorError("dynamic_bass", "dynamic_bass must be an object")
    unknown = [str(name) for name in value if name not in _FIELDS]
    if unknown:
        advice = ("; apply a tune whose bass section is linkwitz_transform with delta_highpass_hz (ADR-0359)"
                  if unknown[0] in _ADR_0352_FIELDS else "")
        raise DynamicBassDescriptorError(unknown[0], f"{unknown[0]} is not a bass section field{advice}")
    if missing := sorted(REQUIRED_FIELDS - set(value)):
        raise DynamicBassDescriptorError("dynamic_bass", f"dynamic_bass is missing {', '.join(missing)}")
    return DynamicBassDescriptor(**dict(value)).payload()


def as_dynamic_bass_descriptor(section: DynamicBassDescriptor | Mapping[str, Any]) -> DynamicBassDescriptor:
    """A stored bass section as the descriptor the graph and the model read."""
    if isinstance(section, DynamicBassDescriptor):
        return section
    return DynamicBassDescriptor(**validate_dynamic_bass_descriptor(section))


def boost_biquad(descriptor: DynamicBassDescriptor) -> dict[str, Any]:
    """The CamillaDSP biquad each boost lane plays: the descriptor's Linkwitz transform."""
    shape = descriptor.linkwitz_transform
    return {"type": "LinkwitzTransform", "freq_act": shape.source_hz, "q_act": shape.source_q,
            "freq_target": shape.target_hz, "q_target": shape.target_q}


def _linkwitz_coeffs(parameters: Mapping[str, float]) -> tuple[float, float, float, float, float, float]:
    """CamillaDSP v4.1.3 ``src/filters/biquad.rs`` LinkwitzTransform."""
    d0, d1 = (2.0 * math.pi * parameters["freq_act"]) ** 2, 2.0 * math.pi * parameters["freq_act"] / parameters["q_act"]
    c0 = (2.0 * math.pi * parameters["freq_target"]) ** 2
    c1 = 2.0 * math.pi * parameters["freq_target"] / parameters["q_target"]
    fc = (parameters["freq_target"] + parameters["freq_act"]) / 2.0
    gn = 2.0 * math.pi * fc / math.tan(math.pi * fc / RESPONSE_SAMPLE_RATE_HZ)
    cci = c0 + gn * c1 + gn * gn
    return ((d0 + gn * d1 + gn * gn) / cci, 2.0 * (d0 - gn * gn) / cci, (d0 - gn * d1 + gn * gn) / cci,
            1.0, 2.0 * (c0 - gn * gn) / cci, (c0 - gn * c1 + gn * gn) / cci)


def _delta_response(descriptor: DynamicBassDescriptor, frequencies: list[float]) -> list[complex]:
    """The added bass before compression: HP * (T - 1), T the boost biquad."""
    trig = freq_trig(frequencies)
    boost = biquad_response_complex(_linkwitz_coeffs(boost_biquad(descriptor)), trig)
    highpass = filter_response_complex(
        FilterSpec("delta_highpass", "Highpass", descriptor.delta_highpass_hz, 0.0, SHELF_Q), frequencies, trig,
    )
    return [(value - 1.0) * hp for value, hp in zip(boost, highpass)]


def expected_boost_db(descriptor: DynamicBassDescriptor, freqs_hz: Iterable[float]) -> list[float]:
    """|1 + HP * (T - 1)| in dB, the boost at every volume while the compressor is idle."""
    return [20.0 * math.log10(abs(1.0 + value)) for value in _delta_response(descriptor, list(freqs_hz))]


def dynamic_bass_gain_reserve_db(section: DynamicBassDescriptor | Mapping[str, Any]) -> float:
    """Peak of 1 + |delta|, which bounds every compressor gain; no section adds nothing. The final limiter owns sample peaks."""
    if not section:
        return 0.0
    return _reserve_db(as_dynamic_bass_descriptor(section))


@functools.lru_cache(maxsize=32)
def _reserve_db(descriptor: DynamicBassDescriptor) -> float:
    steps = int(48 * math.log2(RESPONSE_SAMPLE_RATE_HZ / 2.0)) + 1
    grid = [2.0 ** (step / 48.0) for step in range(steps)]
    peak = max(abs(value) for value in _delta_response(descriptor, grid))
    return 20.0 * math.log10(1.0 + peak) + _RESERVE_GRID_MARGIN_DB
