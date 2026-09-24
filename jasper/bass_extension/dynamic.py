# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Measured, bounded inputs for CamillaDSP's native dynamic-bass block."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, fields
from typing import Any

from jasper.biquad import (
    RESPONSE_SAMPLE_RATE_HZ, SHELF_Q, FilterSpec, biquad_response_complex, filter_response_complex, freq_trig,
)
from jasper.json_fields import finite_float


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
LINKWITZ_SOURCE_HZ_MIN = DETECTOR_CORNER_HZ_MIN
LINKWITZ_SOURCE_HZ_MAX = DETECTOR_CORNER_HZ_MAX
LINKWITZ_TARGET_HZ_MIN = DELTA_HIGHPASS_HZ_MIN
LINKWITZ_Q_MIN = 0.3
LINKWITZ_Q_MAX = 1.5
# Keeps the LowshelfFO that places the transform's delta zero far below Nyquist.
LINKWITZ_DELTA_ZERO_HZ_MAX = 20000.0
# A 1/48-octave grid under-reads a Q <= 1.5 delta peak by < 0.003 dB (ADR-0352).
_RESERVE_GRID_MARGIN_DB = 0.01

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
    "linkwitz_transform",
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
class LinkwitzTransform:
    """The full-boost shape: the woofer's alignment (source) moved to the target (ADR-0352)."""

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
        if not self.delta_zero_hz <= LINKWITZ_DELTA_ZERO_HZ_MAX:
            raise DynamicBassDescriptorError("linkwitz_transform",
                f"linkwitz_transform's delta zero must be in (0, {LINKWITZ_DELTA_ZERO_HZ_MAX:g}] Hz")

    @property
    def delta_zero_hz(self) -> float:
        """The real zero of T - 1; infinite when no left-half-plane zero exists."""
        damping = self.source_hz / self.source_q - self.target_hz / self.target_q
        return (self.source_hz ** 2 - self.target_hz ** 2) / damping if damping > 0 else math.inf


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
    linkwitz_transform: LinkwitzTransform | None = None

    def __post_init__(self) -> None:
        values = {
            name: _finite(getattr(self, name), name)
            for name in (field.name for field in fields(self)
                         if field.name not in {"delta_highpass_hz", "linkwitz_transform"})
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
        shape = self.linkwitz_transform
        if shape is not None:
            if isinstance(shape, Mapping):
                if set(shape) != {field.name for field in fields(LinkwitzTransform)}:
                    raise DynamicBassDescriptorError("linkwitz_transform",
                        "linkwitz_transform needs exactly source_hz, source_q, target_hz and target_q")
                shape = LinkwitzTransform(**shape)
            elif not isinstance(shape, LinkwitzTransform):
                raise DynamicBassDescriptorError("linkwitz_transform", "linkwitz_transform must be an object")
            if self.delta_highpass_hz is None:
                raise DynamicBassDescriptorError("delta_highpass_hz", "a shaped boost needs its delta high-pass")
            object.__setattr__(self, "linkwitz_transform", shape)


def validate_dynamic_bass_descriptor(value: Any) -> dict[str, Any]:
    """Return the normalized strict candidate payload or raise ``ValueError``."""

    if not isinstance(value, Mapping):
        raise DynamicBassDescriptorError("dynamic_bass", "dynamic_bass must be an object")
    keys = set(value)
    if not _REQUIRED_FIELDS <= keys or not keys <= _REQUIRED_FIELDS | _OPTIONAL_FIELDS:
        raise DynamicBassDescriptorError("dynamic_bass", "dynamic_bass has unknown or missing fields")
    descriptor = DynamicBassDescriptor(**dict(value))
    payload = {name: getattr(descriptor, name) for name in sorted(_REQUIRED_FIELDS | _OPTIONAL_FIELDS)}
    # An unshaped payload carries no shape key, so banked candidate fingerprints do not move.
    if descriptor.linkwitz_transform is None:
        del payload["linkwitz_transform"]
    else:
        payload["linkwitz_transform"] = asdict(descriptor.linkwitz_transform)
    return payload


def loudness_boost_db(canonical_volume_db: float, descriptor: DynamicBassDescriptor) -> float:
    """CamillaDSP v4.1.3's exact 20 dB Loudness interpolation law."""

    level = _finite(canonical_volume_db, "canonical_volume_db")
    fraction = max(0.0, min(1.0, (descriptor.reference_level_db - level) / LOUDNESS_TAPER_DB))
    return descriptor.low_boost_db * fraction


@dataclass(frozen=True)
class DeltaShape:
    """Delta-path stage that turns the full-boost shelf delta into the transform's (ADR-0352)."""

    gain_db: float
    poles: dict[str, Any]
    zero: dict[str, Any]


def delta_shape(descriptor: DynamicBassDescriptor) -> DeltaShape | None:
    """Return S = (T - 1) / (L - 1) at full boost as a mixer gain and two CamillaDSP biquads."""
    transform = descriptor.linkwitz_transform
    if transform is None:
        return None
    # RBJ slope-12 shelf, sqrt(A) = 10**(B/80): L - 1 has poles at corner/sqrt(A), Q = SHELF_Q,
    # a zero at corner*SHELF_Q*(sqrt(A) + 1/sqrt(A)) and DC value 10**(B/20) - 1.
    root = 10.0 ** (descriptor.low_boost_db / 80.0)
    pole_hz = NATIVE_LOUDNESS_CORNER_HZ / root
    zero_ratio = transform.delta_zero_hz / (NATIVE_LOUDNESS_CORNER_HZ * SHELF_Q * (root + 1.0 / root))
    dc_ratio = ((transform.source_hz / transform.target_hz) ** 2 - 1.0) / (root ** 4 - 1.0)
    return DeltaShape(
        gain_db=20.0 * math.log10(dc_ratio * (transform.target_hz / pole_hz) ** 2 / zero_ratio),
        poles={"type": "LinkwitzTransform", "freq_act": pole_hz, "q_act": SHELF_Q,
               "freq_target": transform.target_hz, "q_target": transform.target_q},
        zero={"type": "LowshelfFO", "freq": transform.delta_zero_hz / math.sqrt(zero_ratio),
              "gain": 20.0 * math.log10(zero_ratio)},
    )


def _linkwitz_coeffs(poles: Mapping[str, float]) -> tuple[float, float, float, float, float, float]:
    """CamillaDSP v4.1.3 ``src/filters/biquad.rs`` LinkwitzTransform."""
    d0, d1 = (2.0 * math.pi * poles["freq_act"]) ** 2, 2.0 * math.pi * poles["freq_act"] / poles["q_act"]
    c0, c1 = (2.0 * math.pi * poles["freq_target"]) ** 2, 2.0 * math.pi * poles["freq_target"] / poles["q_target"]
    fc = (poles["freq_target"] + poles["freq_act"]) / 2.0
    gn = 2.0 * math.pi * fc / math.tan(math.pi * fc / RESPONSE_SAMPLE_RATE_HZ)
    cci = c0 + gn * c1 + gn * gn
    return ((d0 + gn * d1 + gn * gn) / cci, 2.0 * (d0 - gn * gn) / cci, (d0 - gn * d1 + gn * gn) / cci,
            1.0, 2.0 * (c0 - gn * gn) / cci, (c0 - gn * c1 + gn * gn) / cci)


def _lowshelf_fo_coeffs(zero: Mapping[str, float]) -> tuple[float, float, float, float, float, float]:
    """CamillaDSP v4.1.3 ``src/filters/biquad.rs`` LowshelfFO."""
    tn = math.tan(math.pi * zero["freq"] / RESPONSE_SAMPLE_RATE_HZ)
    amp = 10.0 ** (zero["gain"] / 40.0)
    return amp * amp * tn + amp, amp * amp * tn - amp, 0.0, tn + amp, tn - amp, 0.0


def _delta_response(
    descriptor: DynamicBassDescriptor, boost_db: float, frequencies: list[float],
    trig: list[tuple[float, float, float, float]],
) -> list[complex]:
    """The added bass before compression: HP * S * (L - 1), S = 1 when unshaped."""
    shelf = filter_response_complex(
        FilterSpec("native_low", "Lowshelf", NATIVE_LOUDNESS_CORNER_HZ, boost_db), frequencies, trig,
    )
    delta = [low - 1.0 for low in shelf]
    shape = delta_shape(descriptor)
    if shape is not None:
        gain = 10.0 ** (shape.gain_db / 20.0)
        poles = biquad_response_complex(_linkwitz_coeffs(shape.poles), trig)
        zero = biquad_response_complex(_lowshelf_fo_coeffs(shape.zero), trig)
        delta = [value * gain * p * z for value, p, z in zip(delta, poles, zero)]
    if descriptor.delta_highpass_hz is not None:
        highpass = filter_response_complex(
            FilterSpec("delta_highpass", "Highpass", descriptor.delta_highpass_hz, 0.0, SHELF_Q), frequencies, trig,
        )
        delta = [value * hp for value, hp in zip(delta, highpass)]
    return delta


def expected_boost_db(
    descriptor: DynamicBassDescriptor, fader_db: float, freqs_hz: Iterable[float],
) -> list[float]:
    frequencies = list(freqs_hz)
    delta = _delta_response(descriptor, loudness_boost_db(fader_db, descriptor), frequencies, freq_trig(frequencies))
    return [20.0 * math.log10(abs(1.0 + value)) for value in delta]


def dynamic_bass_gain_reserve_db(descriptor: DynamicBassDescriptor, fader_db: float | None = None) -> float:
    """Bound the static filter/delta gain at ``fader_db`` (default: full boost); the final limiter owns sample peaks."""
    boost = descriptor.low_boost_db if fader_db is None else loudness_boost_db(fader_db, descriptor)
    if boost <= 0.0:
        return 0.0
    if descriptor.linkwitz_transform is None:
        # Native slope-12 shelf: |H-1| <= sqrt((2+sqrt(5))/4) * (10**(B/20)-1).
        # A Butterworth delta high-pass and gain-only compression cannot enlarge its L2 norm.
        delta_ratio = math.sqrt((2.0 + math.sqrt(5.0)) / 4.0)
        delta_gain = 10.0 ** (boost / 20.0) - 1.0
        return 20.0 * math.log10(1.0 + delta_ratio * delta_gain)
    steps = int(48 * math.log2(RESPONSE_SAMPLE_RATE_HZ / 2.0)) + 1
    grid = [2.0 ** (step / 48.0) for step in range(steps)]
    peak = max(abs(value) for value in _delta_response(descriptor, boost, grid, freq_trig(grid)))
    return 20.0 * math.log10(1.0 + peak) + _RESERVE_GRID_MARGIN_DB
