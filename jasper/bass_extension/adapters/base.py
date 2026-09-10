# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared data contracts for enclosure adapters."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping, Protocol, TypeAlias, TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from jasper.bass_extension.targets import MarginPolicy
    from .sealed import SealedPlantFit

    PlantFit: TypeAlias = SealedPlantFit


class BassExtensionRefusal(StrEnum):
    BASS_OWNER_AMBIGUOUS = "bass_extension_bass_owner_ambiguous"
    ENCLOSURE_UNKNOWN = "bass_extension_enclosure_unknown"
    ENCLOSURE_UNSUPPORTED = "bass_extension_enclosure_unsupported"
    PLANT_UNRESOLVED = "bass_extension_plant_unresolved"
    FIT_QUALITY_INSUFFICIENT = "bass_extension_fit_quality_insufficient"


class CaptureRole(StrEnum):
    WOOFER_NEARFIELD = "woofer_nearfield"
    # The seat-cube median, room gain included: the in-situ fit
    # (ADR-0260 section 3).
    SEAT_MEDIAN = "seat_median"


@dataclass(frozen=True)
class MagnitudeCurve:
    freqs_hz: tuple[float, ...]
    magnitude_db: tuple[float, ...]


@dataclass(frozen=True)
class CabinetInfo:
    enclosure_kind: str
    radiator_count: int | None
    effective_radiating_diameter_mm: float | None
    baffle_width_mm: float | None


COMMISSION_FLOOR_HZ = 20.0
# Leave room for response-grid sampling and four-decimal CamillaDSP emission.
TARGET_RESPONSE_RESERVE_DB = 0.01


def target_response_grid() -> np.ndarray:
    """Dense grid used to bound an emitted target's actual filter gain."""

    return np.concatenate(([0.0], np.geomspace(1.0, 1_000.0, 8192)))


def woofer_curve(
    captures: Mapping[CaptureRole, MagnitudeCurve],
) -> MagnitudeCurve | None:
    """The curve every plant fit reads: the seat median (ADR-0260 section 3),
    or a nearfield capture where one is supplied instead."""

    return captures.get(
        CaptureRole.SEAT_MEDIAN, captures.get(CaptureRole.WOOFER_NEARFIELD)
    )


MIN_CURVE_POINTS = 8


def _curve_arrays(curve: MagnitudeCurve) -> tuple[np.ndarray, np.ndarray]:
    freqs = np.asarray(curve.freqs_hz, dtype=np.float64)
    magnitude = np.asarray(curve.magnitude_db, dtype=np.float64)
    if (
        freqs.ndim != 1
        or len(freqs) != len(magnitude)
        or len(freqs) < MIN_CURVE_POINTS
        or not np.all(np.isfinite(freqs))
        or not np.all(np.isfinite(magnitude))
        or np.any(freqs <= 0.0)
        or np.any(np.diff(freqs) <= 0.0)
    ):
        raise ValueError("magnitude curve must be finite, ascending, and matched")
    return freqs, magnitude


def passband_normalize(freqs: np.ndarray, magnitude: np.ndarray) -> np.ndarray:
    passband = (freqs >= 200.0) & (freqs <= 400.0)
    if not np.any(passband):
        passband = np.arange(len(freqs)) >= max(0, int(0.8 * len(freqs)))
    return magnitude - float(np.mean(magnitude[passband]))


@dataclass(frozen=True)
class FitRefusal:
    refusal: str
    detail: str


@dataclass(frozen=True)
class TargetSpec:
    target_id: str
    fp_hz: float
    qp: float | None
    filters: tuple[Mapping[str, Any], ...]
    boost_headroom_db: float
    subsonic: Mapping[str, Any] | None


class EnclosureAdapter(Protocol):
    adapter_id: str

    def fit_plant(
        self,
        captures: Mapping[CaptureRole, MagnitudeCurve],
        cabinet: CabinetInfo,
    ) -> PlantFit | FitRefusal: ...

    def generate_family(
        self,
        plant: PlantFit,
        *,
        margin: "MarginPolicy",
        n_targets: int = 5,
    ) -> tuple[TargetSpec, ...]: ...

    def predicted_response(
        self,
        plant: PlantFit,
        target: TargetSpec,
        freqs_hz: np.ndarray,
    ) -> np.ndarray: ...
