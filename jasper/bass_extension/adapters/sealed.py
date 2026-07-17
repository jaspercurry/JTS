# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Second-order sealed-box fit and Linkwitz-Transform family."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, ClassVar, Mapping, TYPE_CHECKING

import numpy as np
from scipy.optimize import least_squares

from jasper.audio_measurement.analysis import smooth_fractional_octave
from jasper.bass_extension.alignment import (
    boost_headroom_db,
    butterworth_highpass_db,
    linkwitz_transform_params,
    lt_response_db,
    second_order_highpass_db,
)
from .base import (
    COMMISSION_FLOOR_HZ,
    CabinetInfo,
    CaptureRole,
    FitRefusal,
    MagnitudeCurve,
    TargetSpec,
)

if TYPE_CHECKING:
    from jasper.bass_extension.targets import MarginPolicy


@dataclass(frozen=True)
class SealedPlantFit:
    f0_hz: float
    q0: float
    fit_rms_db: float
    notes: tuple[str, ...] = ()

    adapter_id: ClassVar[str] = "sealed_v1"
    adapter_version: ClassVar[int] = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "f0_hz": self.f0_hz,
            "q0": self.q0,
            "fit_rms_db": self.fit_rms_db,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SealedPlantFit":
        keys = {"f0_hz", "q0", "fit_rms_db", "notes"}
        if not isinstance(value, Mapping) or set(value) != keys:
            raise ValueError("sealed plant fit schema is invalid")
        converted = []
        for key in ("f0_hz", "q0", "fit_rms_db"):
            raw = value[key]
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise ValueError(f"sealed plant fit {key} must be finite numeric")
            number = float(raw)
            if not math.isfinite(number):
                raise ValueError(f"sealed plant fit {key} must be finite numeric")
            converted.append(number)
        if not (
            15.0 <= converted[0] <= 200.0
            and 0.3 <= converted[1] <= 1.5
            and converted[2] >= 0.0
        ):
            raise ValueError("sealed plant fit values are outside the valid domain")
        notes = value["notes"]
        if not isinstance(notes, (list, tuple)) or not all(
            isinstance(note, str) for note in notes
        ):
            raise ValueError("sealed plant fit notes must be strings")
        return cls(
            f0_hz=converted[0],
            q0=converted[1],
            fit_rms_db=converted[2],
            notes=tuple(notes),
        )


def _curve_arrays(curve: MagnitudeCurve) -> tuple[np.ndarray, np.ndarray]:
    freqs = np.asarray(curve.freqs_hz, dtype=np.float64)
    magnitude = np.asarray(curve.magnitude_db, dtype=np.float64)
    if (
        freqs.ndim != 1
        or len(freqs) != len(magnitude)
        or len(freqs) < 8
        or not np.all(np.isfinite(freqs))
        or not np.all(np.isfinite(magnitude))
        or np.any(freqs <= 0.0)
        or np.any(np.diff(freqs) <= 0.0)
    ):
        raise ValueError("magnitude curve must be finite, ascending, and matched")
    return freqs, magnitude


def _passband_normalize(freqs: np.ndarray, magnitude: np.ndarray) -> np.ndarray:
    passband = (freqs >= 200.0) & (freqs <= 400.0)
    if not np.any(passband):
        passband = np.arange(len(freqs)) >= max(0, int(0.8 * len(freqs)))
    return magnitude - float(np.mean(magnitude[passband]))


def _minus_six_estimate(freqs: np.ndarray, magnitude: np.ndarray) -> float:
    candidates = np.flatnonzero((magnitude[:-1] <= -6.0) & (magnitude[1:] > -6.0))
    if candidates.size:
        i = int(candidates[-1])
        fraction = (-6.0 - magnitude[i]) / (magnitude[i + 1] - magnitude[i])
        return float(np.exp(
            np.log(freqs[i]) + fraction * (np.log(freqs[i + 1]) - np.log(freqs[i]))
        ))
    return float(freqs[int(np.argmin(np.abs(magnitude + 6.0)))])


def _fit_model(
    freqs: np.ndarray,
    magnitude: np.ndarray,
    f0_start: float,
    *,
    order: int,
) -> tuple[np.ndarray, float]:
    in_window = (freqs >= 0.3 * f0_start) & (freqs <= 3.0 * f0_start)
    if np.count_nonzero(in_window) < 6:
        raise ValueError("sealed fit window has insufficient support")
    fit_freqs = freqs[in_window]
    observed = magnitude[in_window]

    def residual(params: np.ndarray) -> np.ndarray:
        f0, q, offset = params
        model = (
            second_order_highpass_db(fit_freqs, f0, q)
            if order == 2
            else butterworth_highpass_db(fit_freqs, f0, 3)
        )
        return model + offset - observed

    result = least_squares(
        residual,
        x0=(min(max(f0_start, 15.0), 200.0), 0.707, 0.0),
        bounds=((15.0, 0.3, -np.inf), (200.0, 1.5, np.inf)),
    )
    rms = float(np.sqrt(np.mean(residual(result.x) ** 2)))
    return result.x, rms


class SealedAdapter:
    adapter_id = "sealed_v1"
    adapter_version = 1
    required_captures = (CaptureRole.WOOFER_NEARFIELD,)

    def fit_plant(
        self,
        captures: Mapping[CaptureRole, MagnitudeCurve],
        cabinet: CabinetInfo,
    ) -> SealedPlantFit | FitRefusal:
        curve = captures.get(CaptureRole.WOOFER_NEARFIELD)
        if curve is None:
            return FitRefusal(
                "bass_extension_fit_quality_insufficient",
                "woofer nearfield capture is required",
            )
        freqs, magnitude = _curve_arrays(curve)
        normalized = _passband_normalize(freqs, magnitude)
        smoothed = smooth_fractional_octave(freqs, normalized)
        estimate = _minus_six_estimate(freqs, smoothed)
        first, _ = _fit_model(freqs, smoothed, estimate, order=2)
        second, rms = _fit_model(freqs, smoothed, float(first[0]), order=2)
        _, third_rms = _fit_model(freqs, smoothed, float(second[0]), order=3)
        if third_rms + 0.5 < rms:
            return FitRefusal(
                "bass_extension_fit_quality_insufficient",
                "third-order rolloff fits better; check for cabinet leakage or stuffing",
            )
        if rms > 1.5:
            return FitRefusal(
                "bass_extension_fit_quality_insufficient",
                f"sealed second-order fit residual is {rms:.2f} dB RMS",
            )
        f0_hz = float(second[0])
        return SealedPlantFit(
            f0_hz=f0_hz,
            q0=float(second[1]),
            fit_rms_db=rms,
            notes=("already_at_floor",)
            if COMMISSION_FLOOR_HZ >= 0.99 * f0_hz
            else (),
        )

    def generate_family(
        self,
        plant: SealedPlantFit,
        *,
        margin: MarginPolicy,
        n_targets: int = 5,
    ) -> tuple[TargetSpec, ...]:
        if n_targets < 2:
            raise ValueError("n_targets must include an extended and natural member")
        deepest = max(
            COMMISSION_FLOOR_HZ,
            plant.f0_hz / 10.0 ** (margin.boost_cap_db / 40.0),
        )
        subsonic = {
            "type": "ButterworthHighpass",
            "freq": max(15.0, 0.5 * deepest),
            "order": 2,
        }
        natural = TargetSpec(
            target_id="natural",
            fp_hz=plant.f0_hz,
            qp=plant.q0,
            filters=(),
            boost_headroom_db=0.0,
            subsonic=dict(subsonic),
        )
        if plant.q0 > 1.2 or deepest >= 0.99 * plant.f0_hz:
            return (natural,)

        corners = np.geomspace(deepest, plant.f0_hz, n_targets)
        grid = np.geomspace(10.0, 500.0, 960)
        natural_chain = second_order_highpass_db(grid, plant.f0_hz, plant.q0)
        natural_chain += butterworth_highpass_db(
            grid, float(subsonic["freq"]), int(subsonic["order"])
        )
        family = []
        for fp in corners[:-1]:
            fp = float(fp)
            target_chain = natural_chain + lt_response_db(
                grid, plant.f0_hz, plant.q0, fp, 0.65
            )
            family.append(TargetSpec(
                target_id=f"t{fp:.2f}".rstrip("0").rstrip("."),
                fp_hz=fp,
                qp=0.65,
                filters=(linkwitz_transform_params(
                    plant.f0_hz, plant.q0, fp, 0.65
                ),),
                boost_headroom_db=boost_headroom_db(
                    target_chain, natural_chain
                ),
                subsonic=dict(subsonic),
            ))
        family.append(natural)
        return tuple(family)

    def predicted_response(
        self,
        plant: SealedPlantFit,
        target: TargetSpec,
        freqs_hz: np.ndarray,
    ) -> np.ndarray:
        freqs = np.asarray(freqs_hz, dtype=np.float64)
        response = second_order_highpass_db(freqs, plant.f0_hz, plant.q0)
        if target.filters:
            assert target.qp is not None
            response = response + lt_response_db(
                freqs,
                plant.f0_hz,
                plant.q0,
                target.fp_hz,
                float(target.qp),
            )
        if target.subsonic is not None:
            response = response + butterworth_highpass_db(
                freqs,
                float(target.subsonic["freq"]),
                int(target.subsonic["order"]),
            )
        return response


SEALED_ADAPTER = SealedAdapter()
