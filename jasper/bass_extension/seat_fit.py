# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Fit the bass plant on the seat-cube median and size the family it supports.

There is no nearfield rung here: the plant is fitted on the median of the seat
cube, in situ, room gain included (ADR-0260 section 3). Every number published
comes from the enclosure adapter, :mod:`jasper.bass_extension.alignment` and
:mod:`jasper.bass_extension.targets` — this module masks, smooths and joins,
and invents no physics of its own.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping, TYPE_CHECKING

import numpy as np

from jasper.audio_measurement.analysis import smooth_fractional_octave
from jasper.bass_extension.adapters import SEALED_ADAPTER, adapter_for_enclosure
from jasper.bass_extension.adapters.base import (
    MIN_CURVE_POINTS,
    CabinetInfo,
    CaptureRole,
    EnclosureAdapter,
    FitRefusal,
    MagnitudeCurve,
    TargetSpec,
    passband_normalize,
)
from jasper.bass_extension.adapters.sealed import SealedPlantFit, declared_plant
from jasper.bass_extension.alignment import lt_boost_db
from jasper.bass_extension.profile import BassExtensionRefusal
from jasper.bass_extension.targets import MarginPolicy, digital_anchor_level
from jasper.json_fields import finite_float

if TYPE_CHECKING:
    from jasper.bass_extension.adapters.base import PlantFit

#: 1/3 octave: the in-room median carries modal ripple the adapter's own
#: residual gate (sealed.py's 1.5 dB) was not written for.
_SMOOTHING_FRACTION = 3

#: The role that owns the bass when the topology declares one of its own.
_BASS_ROLE = "subwoofer"


class SeatFitRefused(ValueError):
    """A named refusal with the evidence behind it. Never a bare failure."""

    def __init__(
        self, reason: BassExtensionRefusal | str, detail: Mapping[str, Any]
    ) -> None:
        super().__init__(str(reason))
        self.reason = str(reason)
        self.detail = dict(detail)


@dataclass(frozen=True)
class SeatMedian:
    """One seat cube's median magnitude, and the ceiling it is trusted to."""

    freqs_hz: np.ndarray
    median_db: np.ndarray
    ceiling_hz: float
    ceiling_source: str
    n_positions: int


@dataclass(frozen=True)
class DeclaredPlant:
    """The operator's datasheet plant, standing in for a fit that refused."""

    f0_hz: float
    q0: float


@dataclass(frozen=True)
class Rung:
    """One family member, as the apply door reads it.

    ``lt_boost_db`` is the 40*log10(f0/fp) of a ``LinkwitzTransform`` in the
    target's chain, ``0.0`` where the chain has none: equal excursion is the
    prior. One headroom gain absorbs the whole chain's boost, so the target's
    ``boost_headroom_db`` is the level cost (ADR-0257 section 3).
    """

    target: TargetSpec
    max_listening_level: int
    lt_boost_db: float


@dataclass(frozen=True)
class SeatFit:
    """The fitted plant, the family it supports, and what stood behind both."""

    adapter_id: str
    owner_role: str
    owner_target_id: str
    margin: str
    effective_plant: Mapping[str, Any]
    fit_refusal: Mapping[str, str] | None
    rungs: tuple[Rung, ...]
    curve: Mapping[str, list[float]]
    ceiling_hz: float
    ceiling_source: str
    n_positions: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _unreadable(**detail: Any) -> SeatFitRefused:
    return SeatFitRefused(BassExtensionRefusal.MEDIAN_UNREADABLE, detail)


def _float_array(value: Any, field: str) -> np.ndarray:
    if not isinstance(value, (list, tuple)):
        raise _unreadable(field=field, problem="not_a_list")
    numbers = [finite_float(item) for item in value]
    if any(number is None for number in numbers):
        raise _unreadable(field=field, problem="not_finite_numeric")
    return np.asarray(numbers, dtype=np.float64)


def read_seat_median(payload: Mapping[str, Any]) -> SeatMedian:
    """One ``room_median.json`` document as a curve, or a named refusal.

    The document is the seat lane's room-median view (issue #4502).
    """

    if not isinstance(payload, Mapping):
        raise _unreadable(problem="not_an_object")
    freqs = _float_array(payload.get("freqs_hz"), "freqs_hz")
    median = _float_array(payload.get("median_db"), "median_db")
    if freqs.size != median.size:
        raise _unreadable(problem="length_mismatch", n_freqs=int(freqs.size),
                          n_median=int(median.size))
    if freqs.size == 0 or np.any(freqs <= 0.0) or np.any(np.diff(freqs) <= 0.0):
        raise _unreadable(field="freqs_hz", problem="not_positive_ascending")
    ceiling = finite_float(payload.get("ceiling_hz"))
    if ceiling is None or ceiling <= 0.0:
        raise _unreadable(field="ceiling_hz", problem="not_positive")
    below = int(np.count_nonzero(freqs <= ceiling))
    if below < MIN_CURVE_POINTS:
        raise _unreadable(problem="too_few_points_below_ceiling", n_points=below,
                          min_points=MIN_CURVE_POINTS, ceiling_hz=ceiling)
    positions = payload.get("n_positions")
    if isinstance(positions, bool) or not isinstance(positions, int) or positions < 1:
        raise _unreadable(field="n_positions", problem="not_a_positive_int")
    source = payload.get("ceiling_source")
    if not isinstance(source, str):
        raise _unreadable(field="ceiling_source", problem="ceiling_source_must_be_text")
    return SeatMedian(freqs, median, float(ceiling), source, positions)


def bass_owner_target(safety_profile: Mapping[str, Any]) -> Mapping[str, Any]:
    """Which declared target owns the bass: the subwoofer if one is declared,
    else the target whose hard excitation band reaches lowest."""

    targets = safety_profile.get("targets")
    if not isinstance(targets, (list, tuple)) or not targets:
        raise SeatFitRefused(
            BassExtensionRefusal.ENCLOSURE_UNKNOWN, {"problem": "no_targets"}
        )
    subwoofers = [target for target in targets if target.get("role") == _BASS_ROLE]
    if len(subwoofers) > 1:
        raise SeatFitRefused(BassExtensionRefusal.BASS_OWNER_AMBIGUOUS, {
            "problem": "more_than_one_subwoofer",
            "target_ids": [target.get("target_id") for target in subwoofers],
        })
    return subwoofers[0] if subwoofers else min(targets, key=_band_floor_hz)


def _band_floor_hz(target: Mapping[str, Any]) -> float:
    band = target.get("hard_excitation_band_hz")
    floor = finite_float(band[0]) if isinstance(band, (list, tuple)) and band else None
    return float("inf") if floor is None else floor


def cabinet_of(target: Mapping[str, Any]) -> tuple[EnclosureAdapter, CabinetInfo]:
    """The adapter and cabinet this target declares, or a named refusal."""

    cabinet = target.get("cabinet")
    if not isinstance(cabinet, Mapping):
        raise SeatFitRefused(BassExtensionRefusal.ENCLOSURE_UNKNOWN, {
            "target_id": target.get("target_id"), "problem": "no_cabinet",
        })
    kind = str(cabinet.get("enclosure_kind") or "")
    adapter = adapter_for_enclosure(kind)
    if adapter is None:
        raise SeatFitRefused(BassExtensionRefusal.ENCLOSURE_UNSUPPORTED, {
            "target_id": target.get("target_id"), "enclosure_kind": kind,
            "problem": "no_adapter_for_enclosure",
        })
    if CaptureRole.SEAT_MEDIAN not in adapter.required_captures:
        raise SeatFitRefused(BassExtensionRefusal.ENCLOSURE_UNSUPPORTED, {
            "target_id": target.get("target_id"), "enclosure_kind": kind,
            "problem": "adapter_needs_captures_this_door_cannot_take",
            "required_captures": [role.value for role in adapter.required_captures],
        })
    count = cabinet.get("radiator_count")
    return adapter, CabinetInfo(
        enclosure_kind=kind,
        radiator_count=count if isinstance(count, int) and not isinstance(count, bool) else None,
        effective_radiating_diameter_mm=finite_float(
            cabinet.get("effective_radiating_diameter_mm")
        ),
        baffle_width_mm=finite_float(cabinet.get("baffle_width_mm")),
        passive_radiator_diameter_mm=finite_float(
            cabinet.get("passive_radiator_diameter_mm")
        ),
    )


def _rung(target: TargetSpec, f0_hz: float | None, margin: MarginPolicy) -> Rung:
    transformed = any(
        spec.get("type") == "LinkwitzTransform" for spec in target.filters
    )
    return Rung(
        target=target,
        max_listening_level=digital_anchor_level(
            float(target.boost_headroom_db), margin.digital_margin_db
        ),
        lt_boost_db=(
            lt_boost_db(f0_hz, target.fp_hz)
            if transformed and f0_hz is not None else 0.0
        ),
    )


def fit_seat_median(
    median: SeatMedian,
    *,
    adapter: EnclosureAdapter,
    cabinet: CabinetInfo,
    margin: MarginPolicy,
    declared: DeclaredPlant | None,
    owner_role: str,
    owner_target_id: str,
) -> SeatFit:
    """Fit the plant on the median below the ceiling and size its family."""

    below = median.freqs_hz <= median.ceiling_hz
    freqs = median.freqs_hz[below]
    smoothed = smooth_fractional_octave(
        freqs, median.median_db[below], fraction=_SMOOTHING_FRACTION
    )
    curve = MagnitudeCurve(
        tuple(float(value) for value in freqs),
        tuple(float(value) for value in smoothed),
    )

    fitted = adapter.fit_plant({CaptureRole.SEAT_MEDIAN: curve}, cabinet)
    fit_refusal: dict[str, str] | None = None
    plant: PlantFit
    if isinstance(fitted, FitRefusal):
        # A declared f0/Q describes a sealed plant and nothing else, so it can
        # only stand in for the sealed fit.
        if declared is None or adapter is not SEALED_ADAPTER:
            raise SeatFitRefused(BassExtensionRefusal.PLANT_UNRESOLVED, {
                "refusal": fitted.refusal, "detail": fitted.detail,
                "adapter_id": adapter.adapter_id,
                "declared_plant": declared is not None,
            })
        stood_in = declared_plant(declared.f0_hz, declared.q0)
        if isinstance(stood_in, FitRefusal):
            raise SeatFitRefused(BassExtensionRefusal.PLANT_UNRESOLVED, {
                "problem": "declared_plant_outside_domain",
                "error": stood_in.detail,
                "f0_hz": declared.f0_hz, "q0": declared.q0,
            })
        plant = stood_in
        fit_refusal = {"refusal": fitted.refusal, "detail": fitted.detail}
    else:
        plant = fitted

    sealed = plant if isinstance(plant, SealedPlantFit) else None
    f0_hz = None if sealed is None else sealed.f0_hz
    family = adapter.generate_family(plant, margin=margin)
    model = np.asarray(
        adapter.predicted_response(plant, family[-1], freqs), dtype=np.float64
    )
    return SeatFit(
        adapter_id=adapter.adapter_id,
        owner_role=owner_role,
        owner_target_id=owner_target_id,
        margin=margin.name,
        effective_plant={
            "f0_hz": f0_hz,
            "q0": None if sealed is None else sealed.q0,
            "fit_rms_db": None if fit_refusal else plant.fit_rms_db,
            "source": "declared" if fit_refusal else "seat_median_fit",
            "notes": list(plant.notes),
        },
        fit_refusal=fit_refusal,
        rungs=tuple(_rung(target, f0_hz, margin) for target in family),
        curve={
            "freqs_hz": [float(value) for value in freqs],
            "median_smoothed_db": [float(v) for v in passband_normalize(freqs, smoothed)],
            "model_db": [float(v) for v in passband_normalize(freqs, model)],
        },
        ceiling_hz=median.ceiling_hz,
        ceiling_source=median.ceiling_source,
        n_positions=median.n_positions,
    )
