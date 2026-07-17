# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Passive-radiator landmark fit and protected vented-family reuse."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, ClassVar, Mapping, TYPE_CHECKING

import numpy as np

from jasper.audio_measurement.analysis import smooth_fractional_octave
from jasper.bass_extension.alignment import boost_headroom_db
from .base import CabinetInfo, CaptureRole, FitRefusal, MagnitudeCurve, TargetSpec
from .ported import (PortedPlantFit, _curve_arrays, _filters_response_db,
                     fit_ported_plant, generate_ported_family,
                     ported_predicted_response)

if TYPE_CHECKING:
    from jasper.bass_extension.targets import MarginPolicy


@dataclass(frozen=True)
class PassiveRadiatorPlantFit:
    fb_hz: float
    knee_hz: float
    knee_slope_db_oct: float
    fit_rms_db: float
    natural_curve: MagnitudeCurve
    notch_hz: float
    notes: tuple[str, ...] = ()

    adapter_id: ClassVar[str] = "passive_radiator_v1"
    adapter_version: ClassVar[int] = 1

    def to_dict(self) -> dict[str, Any]:
        value = _as_ported(self).to_dict()
        value["notch_hz"] = self.notch_hz
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PassiveRadiatorPlantFit":
        keys = {"fb_hz", "knee_hz", "knee_slope_db_oct", "fit_rms_db",
                "natural_curve", "notch_hz", "notes"}
        if not isinstance(value, Mapping) or set(value) != keys:
            raise ValueError("passive-radiator plant fit schema is invalid")
        try:
            ported = PortedPlantFit.from_dict({key: value[key] for key in (
                "fb_hz", "knee_hz", "knee_slope_db_oct", "fit_rms_db",
                "natural_curve", "notes")})
            raw_notch = value["notch_hz"]
            if (isinstance(raw_notch, bool)
                    or not isinstance(raw_notch, (int, float))):
                raise ValueError
            notch = float(raw_notch)
            if not math.isfinite(notch) or not 10.0 <= notch <= 0.9 * ported.fb_hz:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ValueError("passive-radiator plant fit values are invalid") from exc
        return cls(ported.fb_hz, ported.knee_hz, ported.knee_slope_db_oct,
                   ported.fit_rms_db, ported.natural_curve, notch, ported.notes)


def _as_ported(plant: PassiveRadiatorPlantFit) -> PortedPlantFit:
    return PortedPlantFit(plant.fb_hz, plant.knee_hz,
                          plant.knee_slope_db_oct, plant.fit_rms_db,
                          plant.natural_curve, plant.notes)


class PassiveRadiatorAdapter:
    adapter_id = "passive_radiator_v1"
    adapter_version = 1
    required_captures = (CaptureRole.WOOFER_NEARFIELD, CaptureRole.PR_NEARFIELD)

    def fit_plant(
        self,
        captures: Mapping[CaptureRole, MagnitudeCurve],
        cabinet: CabinetInfo,
    ) -> PassiveRadiatorPlantFit | FitRefusal:
        pr_curve = captures.get(CaptureRole.PR_NEARFIELD)
        if pr_curve is None:
            return FitRefusal("bass_extension_pr_notch_not_located",
                              "passive-radiator nearfield capture is required")
        ported = fit_ported_plant(captures)
        if isinstance(ported, FitRefusal):
            return ported
        woofer_freqs, woofer_db = _curve_arrays(captures[CaptureRole.WOOFER_NEARFIELD])
        pr_freqs, pr_db = _curve_arrays(pr_curve)
        woofer_db = smooth_fractional_octave(woofer_freqs, woofer_db, fraction=24)
        pr_db = smooth_fractional_octave(pr_freqs, pr_db, fraction=24)
        notes = ported.notes
        if (cabinet.effective_radiating_diameter_mm is not None
                and cabinet.passive_radiator_diameter_mm is not None):
            scale = (cabinet.passive_radiator_diameter_mm
                     / cabinet.effective_radiating_diameter_mm)
            pr_db = pr_db + 20.0 * math.log10(scale)
        else:
            notes = (*notes, "pr_nearfield_unscaled")
        band = (woofer_freqs >= 10.0) & (woofer_freqs <= 0.9 * ported.fb_hz)
        if not np.any(band):
            return FitRefusal("bass_extension_pr_notch_not_located",
                              "capture does not cover the passive-radiator notch region")
        interpolated_pr = np.interp(woofer_freqs[band], pr_freqs, pr_db)
        differences = np.abs(woofer_db[band] - interpolated_pr)
        index = int(np.argmin(differences))
        if differences[index] > 3.0:
            return FitRefusal("bass_extension_pr_notch_not_located",
                              "woofer and passive-radiator magnitudes do not approach within 3 dB")
        notch = float(woofer_freqs[band][index])
        return PassiveRadiatorPlantFit(
            ported.fb_hz, ported.knee_hz, ported.knee_slope_db_oct,
            ported.fit_rms_db, ported.natural_curve, notch, notes)

    def generate_family(
        self,
        plant: PassiveRadiatorPlantFit,
        *,
        margin: MarginPolicy,
        n_targets: int = 5,
    ) -> tuple[TargetSpec, ...]:
        base_family = generate_ported_family(
            _as_ported(plant), margin=margin, n_targets=n_targets
        )
        minimum_corner = 1.1 * plant.notch_hz
        grid = np.geomspace(10.0, 500.0, 512)
        family: list[TargetSpec] = []
        for target in base_family:
            filters = tuple(
                {
                    **filter_spec,
                    "q": max(0.7, float(filter_spec["q"])),
                }
                if filter_spec["type"] == "Peaking"
                else filter_spec
                for filter_spec in target.filters
                if filter_spec["type"] in {"Peaking", "ButterworthHighpass"}
            )
            subsonic = dict(target.subsonic or {})
            subsonic["freq"] = max(float(subsonic["freq"]), minimum_corner)
            response_delta = _filters_response_db(grid, filters)
            below_notch = grid <= plant.notch_hz
            if target.target_id != "natural" and np.max(
                response_delta[below_notch]
            ) > 0.5:
                continue
            boost = (
                0.0
                if target.target_id == "natural"
                else boost_headroom_db(
                    response_delta,
                    np.zeros_like(grid),
                )
            )
            family.append(TargetSpec(
                target_id=target.target_id,
                fp_hz=target.fp_hz,
                qp=None,
                filters=filters,
                boost_headroom_db=boost,
                subsonic=subsonic,
            ))
        natural = family.pop()
        family.sort(key=lambda target: (-target.boost_headroom_db, target.fp_hz))
        family.append(natural)
        return tuple(family)

    def predicted_response(
        self,
        plant: PassiveRadiatorPlantFit,
        target: TargetSpec,
        freqs_hz: np.ndarray,
    ) -> np.ndarray:
        return ported_predicted_response(_as_ported(plant), target, freqs_hz)


PASSIVE_RADIATOR_ADAPTER = PassiveRadiatorAdapter()
