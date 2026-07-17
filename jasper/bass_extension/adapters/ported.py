# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Vented-box landmark fit and bounded protected target family."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, ClassVar, Mapping, TYPE_CHECKING

import numpy as np
from scipy.optimize import least_squares

from jasper.audio_measurement.analysis import resample_log, smooth_fractional_octave
from jasper.bass_extension.alignment import (
    boost_headroom_db,
    butterworth_highpass_db,
    low_shelf_response_db,
    peaking_response_db,
)
from .base import (COMMISSION_FLOOR_HZ, CabinetInfo, CaptureRole, FitRefusal,
                   MagnitudeCurve, TargetSpec)

if TYPE_CHECKING:
    from jasper.bass_extension.targets import MarginPolicy


@dataclass(frozen=True)
class PortedPlantFit:
    fb_hz: float
    knee_hz: float
    knee_slope_db_oct: float
    fit_rms_db: float
    natural_curve: MagnitudeCurve
    notes: tuple[str, ...] = ()

    adapter_id: ClassVar[str] = "ported_v1"
    adapter_version: ClassVar[int] = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "fb_hz": self.fb_hz,
            "knee_hz": self.knee_hz,
            "knee_slope_db_oct": self.knee_slope_db_oct,
            "fit_rms_db": self.fit_rms_db,
            "natural_curve": {"freqs_hz": list(self.natural_curve.freqs_hz),
                              "magnitude_db": list(self.natural_curve.magnitude_db)},
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PortedPlantFit":
        keys = {"fb_hz", "knee_hz", "knee_slope_db_oct", "fit_rms_db",
                "natural_curve", "notes"}
        if not isinstance(value, Mapping) or set(value) != keys:
            raise ValueError("ported plant fit schema is invalid")
        numbers = []
        for key in ("fb_hz", "knee_hz", "knee_slope_db_oct", "fit_rms_db"):
            raw = value[key]
            if (isinstance(raw, bool) or not isinstance(raw, (int, float))
                    or not math.isfinite(float(raw))):
                raise ValueError(f"ported plant fit {key} must be finite numeric")
            numbers.append(float(raw))
        if not (15.0 <= numbers[0] <= 120.0
                and numbers[0] < numbers[1] <= 500.0 and numbers[3] >= 0.0):
            raise ValueError("ported plant fit values are outside the valid domain")
        curve = value["natural_curve"]
        if not isinstance(curve, Mapping) or set(curve) != {"freqs_hz", "magnitude_db"}:
            raise ValueError("ported plant fit natural curve schema is invalid")
        frequency_values = curve.get("freqs_hz")
        magnitude_values = curve.get("magnitude_db")
        if not all(isinstance(items, (list, tuple)) and all(
            not isinstance(item, bool) and isinstance(item, (int, float))
            for item in items
        ) for items in (frequency_values, magnitude_values)):
            raise ValueError("ported plant fit natural curve must be numeric")
        try:
            natural_curve = MagnitudeCurve(
                tuple(float(item) for item in frequency_values),
                tuple(float(item) for item in magnitude_values),
            )
            curve_freqs, curve_db = _curve_arrays(natural_curve)
        except (TypeError, ValueError) as exc:
            raise ValueError("ported plant fit natural curve is invalid") from exc
        expected_freqs = np.geomspace(10.0, 500.0, 96)
        passband = (curve_freqs >= 200.0) & (curve_freqs <= 400.0)
        mean_tolerance = (np.finfo(np.float64).eps
                          * max(1.0, float(np.max(np.abs(curve_db))))
                          * np.count_nonzero(passband))
        if len(curve_freqs) != 96 or not np.array_equal(curve_freqs, expected_freqs):
            raise ValueError("ported plant fit natural curve must use the 10-500 Hz grid")
        if abs(float(np.mean(curve_db[passband]))) > mean_tolerance:
            raise ValueError("ported plant fit natural curve must be passband-normalized")
        notes = value["notes"]
        if not isinstance(notes, (list, tuple)) or not all(isinstance(note, str) for note in notes):
            raise ValueError("ported plant fit notes must be strings")
        return cls(*numbers, natural_curve=natural_curve, notes=tuple(notes))


def _curve_arrays(curve: MagnitudeCurve) -> tuple[np.ndarray, np.ndarray]:
    freqs = np.asarray(curve.freqs_hz, dtype=np.float64)
    magnitude = np.asarray(curve.magnitude_db, dtype=np.float64)
    if (freqs.ndim != 1 or len(freqs) != len(magnitude) or len(freqs) < 8
            or not np.all(np.isfinite(freqs)) or not np.all(np.isfinite(magnitude))
            or np.any(freqs <= 0.0) or np.any(np.diff(freqs) <= 0.0)):
        raise ValueError("magnitude curve must be finite, ascending, and matched")
    return freqs, magnitude


def _normalized_curve(curve: MagnitudeCurve) -> tuple[np.ndarray, np.ndarray]:
    freqs, magnitude = _curve_arrays(curve)
    passband = (freqs >= 200.0) & (freqs <= 400.0)
    if not np.any(passband):
        passband = np.arange(len(freqs)) >= max(0, int(0.8 * len(freqs)))
    normalized = magnitude - float(np.mean(magnitude[passband]))
    return freqs, normalized


def _refine_extremum(freqs: np.ndarray, magnitude: np.ndarray, index: int) -> float:
    x = np.log(freqs[index - 1:index + 2])
    y = magnitude[index - 1:index + 2]
    a, b, _ = np.polyfit(x, y, 2)
    if a <= 0.0:
        return float(freqs[index])
    vertex = -b / (2.0 * a)
    if x[0] <= vertex <= x[-1]:
        return float(np.exp(vertex))
    return float(freqs[index])


def _locate_fb(freqs: np.ndarray, magnitude: np.ndarray) -> float | None:
    candidates: list[tuple[float, int]] = []
    for index in range(1, len(freqs) - 1):
        freq = freqs[index]
        if not 15.0 <= freq <= 120.0:
            continue
        if not (magnitude[index] < magnitude[index - 1] and magnitude[index] < magnitude[index + 1]):
            continue
        left = (freqs >= 0.65 * freq) & (freqs <= 0.9 * freq)
        right = (freqs >= 1.1 * freq) & (freqs <= 1.55 * freq)
        if not np.any(left) or not np.any(right):
            continue
        prominence = min(float(np.max(magnitude[left])),
                         float(np.max(magnitude[right]))) - float(magnitude[index])
        if prominence >= 4.0:
            candidates.append((prominence, index))
    if not candidates:
        return None
    _, index = max(candidates)
    return _refine_extremum(freqs, magnitude, index)


def _interpolate_crossing(freqs: np.ndarray, magnitude: np.ndarray,
                          threshold: float, start_hz: float) -> float:
    for index in range(1, len(freqs)):
        if freqs[index] <= start_hz:
            continue
        if magnitude[index - 1] <= threshold < magnitude[index]:
            fraction = ((threshold - magnitude[index - 1])
                        / (magnitude[index] - magnitude[index - 1]))
            return float(np.exp(np.log(freqs[index - 1]) + fraction
                                * (np.log(freqs[index]) - np.log(freqs[index - 1]))))
    return float(freqs[int(np.argmin(np.abs(magnitude - threshold)))])


def _filter_response_db(freqs: np.ndarray, filter_spec: Mapping[str, Any]) -> np.ndarray:
    if filter_spec["type"] == "ButterworthHighpass":
        return butterworth_highpass_db(freqs, float(filter_spec["freq"]),
                                       int(filter_spec["order"]))
    if filter_spec["type"] == "Lowshelf":
        return low_shelf_response_db(freqs, filter_spec["freq"],
                                     filter_spec["q"], filter_spec["gain"])
    if filter_spec["type"] == "Peaking":
        return peaking_response_db(freqs, filter_spec["freq"],
                                   filter_spec["q"], filter_spec["gain"])
    raise ValueError(f"unsupported target filter: {filter_spec['type']}")


def _filters_response_db(freqs: np.ndarray,
                         filters: tuple[Mapping[str, Any], ...]) -> np.ndarray:
    response = np.zeros_like(freqs, dtype=np.float64)
    for filter_spec in filters:
        response += _filter_response_db(freqs, filter_spec)
    return response


def fit_ported_plant(
    captures: Mapping[CaptureRole, MagnitudeCurve],
) -> PortedPlantFit | FitRefusal:
    woofer = captures.get(CaptureRole.WOOFER_NEARFIELD)
    if woofer is None:
        return FitRefusal("bass_extension_tuning_not_located",
                          "woofer nearfield capture is required")
    freqs, measured = _normalized_curve(woofer)
    magnitude = smooth_fractional_octave(freqs, measured, fraction=24)
    fb = _locate_fb(freqs, magnitude)
    if fb is None:
        return FitRefusal("bass_extension_tuning_not_located",
                          "woofer curve has no sharp tuning minimum")
    port = captures.get(CaptureRole.PORT_NEARFIELD)
    if port is not None:
        port_freqs, port_magnitude = _normalized_curve(port)
        port_magnitude = smooth_fractional_octave(port_freqs, port_magnitude,
                                                  fraction=24)
        port_band = (port_freqs >= 15.0) & (port_freqs <= 120.0)
        port_peak = float(port_freqs[port_band][np.argmax(port_magnitude[port_band])])
        if abs(port_peak - fb) > 0.2 * fb:
            return FitRefusal("bass_extension_tuning_not_located",
                              "woofer minimum and port maximum disagree")
    knee = _interpolate_crossing(freqs, magnitude, -3.0, fb)
    slope_center = max(float(freqs[0]), knee / 2.0)
    low = float(np.interp(slope_center / 2.0 ** (1.0 / 12.0), freqs, magnitude))
    high = float(np.interp(slope_center * 2.0 ** (1.0 / 12.0), freqs, magnitude))
    slope = (high - low) * 6.0
    model = np.minimum(0.0, slope * np.log2(np.maximum(freqs, 1e-9) / knee))
    fit_band = (freqs >= 1.2 * fb) & (freqs <= 2.0 * knee)
    rms = float(np.sqrt(np.mean((magnitude[fit_band] - model[fit_band]) ** 2)))
    natural_freqs, natural_db = resample_log(
        freqs, measured, f_min=10.0, f_max=500.0, n_points=96)
    passband = (natural_freqs >= 200.0) & (natural_freqs <= 400.0)
    natural_db -= float(np.mean(natural_db[passband]))
    natural_curve = MagnitudeCurve(tuple(float(freq) for freq in natural_freqs),
                                   tuple(float(level) for level in natural_db))
    return PortedPlantFit(fb, knee, slope, rms, natural_curve)


def _deep_shaping_filters(plant: PortedPlantFit) -> tuple[Mapping[str, Any], ...]:
    freqs = np.geomspace(1.2 * plant.fb_hz, 2.0 * plant.knee_hz, 128)
    natural = np.interp(freqs, plant.natural_curve.freqs_hz,
                        plant.natural_curve.magnitude_db)
    shelf_lo = 1.2 * plant.fb_hz
    shelf_hi = max(shelf_lo * 1.001, plant.knee_hz)

    def residual(params: np.ndarray) -> np.ndarray:
        shelf_gain, shelf_freq, shelf_q, peak_gain, peak_q = params
        return (
            natural
            + low_shelf_response_db(freqs, shelf_freq, shelf_q, shelf_gain)
            + peaking_response_db(freqs, plant.knee_hz, peak_q, peak_gain)
        )

    result = least_squares(
        residual,
        x0=(3.0, math.sqrt(shelf_lo * shelf_hi), 0.707, 1.5, 0.707),
        bounds=((0.0, shelf_lo, 0.4, 0.0, 0.4), (6.0, shelf_hi, 1.5, 6.0, 1.5)),
    )
    shelf_gain, shelf_freq, shelf_q, peak_gain, peak_q = result.x
    filters: list[Mapping[str, Any]] = []
    if shelf_gain > 1e-3:
        filters.append({"type": "Lowshelf", "freq": float(shelf_freq),
                        "gain": float(shelf_gain), "q": float(shelf_q)})
    if peak_gain > 1e-3:
        filters.append({"type": "Peaking", "freq": plant.knee_hz,
                        "gain": float(peak_gain), "q": float(peak_q)})
    return tuple(filters)


def generate_ported_family(
    plant: PortedPlantFit,
    *,
    margin: MarginPolicy,
    n_targets: int = 5,
) -> tuple[TargetSpec, ...]:
    if n_targets < 2:
        raise ValueError("n_targets must include an extended and natural member")
    base_corner = max(
        COMMISSION_FLOOR_HZ, margin.subsonic_corner_ratio * plant.fb_hz
    )
    subsonic = {
        "type": "ButterworthHighpass",
        "freq": base_corner,
        "order": margin.subsonic_order,
    }
    natural = TargetSpec(
        target_id="natural",
        fp_hz=max(plant.knee_hz, base_corner),
        qp=None,
        filters=(),
        boost_headroom_db=0.0,
        subsonic=dict(subsonic),
    )
    if base_corner >= 0.99 * plant.knee_hz:
        return (natural,)
    deep_filters = _deep_shaping_filters(plant)
    non_natural_count = n_targets - 1
    corners = np.geomspace(base_corner, plant.knee_hz, non_natural_count)
    grid = np.geomspace(10.0, 500.0, 512)
    family: list[TargetSpec] = []
    for index, corner in enumerate(corners):
        scale = (
            1.0 - index / (non_natural_count - 1)
            if non_natural_count > 1 else 1.0
        )
        shelf_scale = 1.0 if index == 0 else 0.5 if index == 1 else 0.0
        filters: list[Mapping[str, Any]] = []
        for filter_spec in deep_filters:
            applied_scale = shelf_scale if filter_spec["type"] == "Lowshelf" else scale
            if applied_scale > 0.0:
                filters.append({
                    **filter_spec,
                    "gain": float(filter_spec["gain"]) * applied_scale,
                })
        if index > 0:
            filters.append({
                "type": "ButterworthHighpass",
                "freq": float(corner),
                "order": margin.subsonic_order,
            })
        filter_tuple = tuple(filters)
        boost = boost_headroom_db(
            _filters_response_db(grid, filter_tuple),
            np.zeros_like(grid),
        )
        family.append(TargetSpec(
            target_id=f"t{float(corner):.2f}".rstrip("0").rstrip("."),
            fp_hz=float(corner),
            qp=None,
            filters=filter_tuple,
            boost_headroom_db=boost,
            subsonic=dict(subsonic),
        ))
    family.sort(key=lambda target: (-target.boost_headroom_db, target.fp_hz))
    family.append(natural)
    return tuple(family)


def ported_predicted_response(
    plant: PortedPlantFit,
    target: TargetSpec,
    freqs_hz: np.ndarray,
) -> np.ndarray:
    freqs = np.asarray(freqs_hz, dtype=np.float64)
    natural = np.interp(
        freqs, plant.natural_curve.freqs_hz, plant.natural_curve.magnitude_db
    )
    return natural + _filters_response_db(freqs, target.filters)


class PortedAdapter:
    adapter_id = "ported_v1"
    adapter_version = 1
    required_captures = (CaptureRole.WOOFER_NEARFIELD,)

    def fit_plant(
        self,
        captures: Mapping[CaptureRole, MagnitudeCurve],
        cabinet: CabinetInfo,
    ) -> PortedPlantFit | FitRefusal:
        return fit_ported_plant(captures)

    def generate_family(
        self,
        plant: PortedPlantFit,
        *,
        margin: MarginPolicy,
        n_targets: int = 5,
    ) -> tuple[TargetSpec, ...]:
        return generate_ported_family(plant, margin=margin, n_targets=n_targets)

    def predicted_response(
        self,
        plant: PortedPlantFit,
        target: TargetSpec,
        freqs_hz: np.ndarray,
    ) -> np.ndarray:
        return ported_predicted_response(plant, target, freqs_hz)


PORTED_ADAPTER = PortedAdapter()
