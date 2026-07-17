# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared data contracts for enclosure adapters."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast, Mapping, Protocol, TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from jasper.bass_extension.targets import MarginPolicy


class CaptureRole(StrEnum):
    WOOFER_NEARFIELD = "woofer_nearfield"
    PORT_NEARFIELD = "port_nearfield"
    PR_NEARFIELD = "pr_nearfield"


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
    passive_radiator_diameter_mm: float | None = None


COMMISSION_FLOOR_HZ = 20.0


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
    limiter_threshold_dbfs: float | None = None


class EnclosureAdapter(Protocol):
    adapter_id: str
    adapter_version: int
    required_captures: tuple[CaptureRole, ...]

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


from .passive_radiator import PASSIVE_RADIATOR_ADAPTER, PassiveRadiatorPlantFit
from .ported import PORTED_ADAPTER, PortedPlantFit
from .sealed import SEALED_ADAPTER, SealedPlantFit


PlantFit = SealedPlantFit | PortedPlantFit | PassiveRadiatorPlantFit


ADAPTERS: dict[str, EnclosureAdapter] = {
    "sealed_v1": cast(EnclosureAdapter, SEALED_ADAPTER),
    "ported_v1": cast(EnclosureAdapter, PORTED_ADAPTER),
    "passive_radiator_v1": cast(EnclosureAdapter, PASSIVE_RADIATOR_ADAPTER),
}


def adapter_for_enclosure(enclosure_kind: str) -> EnclosureAdapter | None:
    adapter_id = {
        "sealed": "sealed_v1",
        "vented": "ported_v1",
        "passive_radiator": "passive_radiator_v1",
    }.get(enclosure_kind)
    return ADAPTERS.get(adapter_id) if adapter_id is not None else None
