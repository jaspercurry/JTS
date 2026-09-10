# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Fit the bass plant on the seat-cube median and size the family it supports.

There is no nearfield rung here: the plant is fitted on the median of the seat
cube, in situ, room gain included (ADR-0260 section 3). Every number published
comes from the enclosure adapter, :mod:`jasper.bass_extension.alignment` and
:mod:`jasper.bass_extension.targets` — this module masks, smooths and joins,
and invents no physics of its own.

The median arrives as a value, never as a document: the room door
(:func:`~jasper.active_speaker.crossover_v2.room_prescription.read_room_median`)
is the one reader of ``room_median.json``, so a median fitted here is exactly
one that door would prescribe against, and the view holds that seam.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any, Mapping, TYPE_CHECKING

import numpy as np

from jasper.audio_measurement.analysis import smooth_fractional_octave
from jasper.bass_extension.adapters import SEALED_ADAPTER, adapter_for_enclosure
from jasper.bass_extension.adapters.base import (
    BassExtensionRefusal,
    CabinetInfo,
    CaptureRole,
    EnclosureAdapter,
    FitRefusal,
    MagnitudeCurve,
    TargetSpec,
    passband_normalize,
)
from jasper.bass_extension.adapters.sealed import declared_plant
from jasper.bass_extension.alignment import lt_boost_db
from jasper.bass_extension.targets import MarginPolicy, digital_anchor_level
from jasper.json_fields import finite_float

if TYPE_CHECKING:
    # Runtime-free on purpose: ``active_speaker`` imports ``bass_extension`` at
    # module level (baseline_profile, runtime_contract), so the value's type is
    # all this module may take from that side.
    from jasper.active_speaker.crossover_v2.room_prescription import RoomMedian

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

    ``max_listening_level`` is the DIGITAL bound and only that: the level at
    which the boost still fits under the margin policy's digital headroom. It
    is no thermal, excursion or acoustic bound on how loud the rung may play.
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
    #: Where the fitted plant rolls off: the natural rung's own corner, which
    #: is the one corner every adapter defines.
    effective_corner_hz: float
    effective_q: float | None
    #: The adapter's own fit record, verbatim, plus where it came from.
    effective_plant: Mapping[str, Any]
    plant_source: str
    #: The fit residual, or ``None`` where a declared plant stood in for it.
    fit_rms_db: float | None
    fit_refusal: Mapping[str, str] | None
    rungs: tuple[Rung, ...]
    #: The median as fitted and the fitted plant's own response, on one
    #: passband rule so their difference is the fit residual and nothing else:
    #: no rung's subsonic is in ``model_db``.
    curve: Mapping[str, list[float]]
    ceiling_hz: float
    ceiling_source: str
    n_positions: int
    #: The level the median was read against, dB. The fit is level-blind — the
    #: adapters passband-normalize what they read — so this is disclosed for a
    #: reader putting the published curves back at measurement level, and is
    #: not an input to any number here.
    level_reference_db: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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
    count = cabinet.get("radiator_count")
    return adapter, CabinetInfo(
        enclosure_kind=kind,
        radiator_count=count if isinstance(count, int) and not isinstance(count, bool) else None,
        effective_radiating_diameter_mm=finite_float(
            cabinet.get("effective_radiating_diameter_mm")
        ),
        baffle_width_mm=finite_float(cabinet.get("baffle_width_mm")),
    )


def _rung(target: TargetSpec, corner_hz: float, margin: MarginPolicy) -> Rung:
    transformed = any(
        spec.get("type") == "LinkwitzTransform" for spec in target.filters
    )
    return Rung(
        target=target,
        max_listening_level=digital_anchor_level(
            float(target.boost_headroom_db), margin.digital_margin_db
        ),
        lt_boost_db=lt_boost_db(corner_hz, target.fp_hz) if transformed else 0.0,
    )


def _plant(
    fitted: PlantFit | FitRefusal,
    *,
    adapter: EnclosureAdapter,
    declared: DeclaredPlant | None,
) -> tuple[PlantFit, dict[str, str] | None]:
    """The plant to size the family from, and the refusal it stood in for."""

    if not isinstance(fitted, FitRefusal):
        return fitted, None
    # A declared f0/Q describes a sealed plant and nothing else, so it can only
    # stand in for the sealed fit.
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
    return stood_in, {"refusal": fitted.refusal, "detail": fitted.detail}


def fit_seat_median(
    median: RoomMedian,
    *,
    adapter: EnclosureAdapter,
    cabinet: CabinetInfo,
    margin: MarginPolicy,
    declared: DeclaredPlant | None,
    owner_role: str,
    owner_target_id: str,
) -> SeatFit:
    """Fit the plant on the median below the ceiling and size its family."""

    # The room door refuses a grid reaching past ``ceiling_hz``, so the whole
    # median is already the band this smooths and fits.
    freqs = median.freqs_hz
    smoothed = smooth_fractional_octave(
        freqs, median.median_db, fraction=_SMOOTHING_FRACTION
    )
    curve = MagnitudeCurve(
        tuple(float(value) for value in freqs),
        tuple(float(value) for value in smoothed),
    )

    plant, fit_refusal = _plant(
        adapter.fit_plant({CaptureRole.SEAT_MEDIAN: curve}, cabinet),
        adapter=adapter, declared=declared,
    )
    family = adapter.generate_family(plant, margin=margin)
    # Every adapter ends its family with the natural alignment: its corner is
    # the plant's own, whatever the enclosure's model calls it.
    natural = family[-1]
    # The model published beside the median is the plant the fit FITTED: the
    # rung's subsonic high-pass is a filter the measurement does not contain,
    # and its skirt in this curve would read as fit error it is not.
    model = np.asarray(
        adapter.predicted_response(plant, replace(natural, subsonic=None), freqs),
        dtype=np.float64,
    )
    return SeatFit(
        adapter_id=adapter.adapter_id,
        owner_role=owner_role,
        owner_target_id=owner_target_id,
        margin=margin.name,
        effective_corner_hz=natural.fp_hz,
        effective_q=natural.qp,
        effective_plant=plant.to_dict(),
        plant_source="declared" if fit_refusal else "seat_median_fit",
        fit_rms_db=None if fit_refusal else plant.fit_rms_db,
        fit_refusal=fit_refusal,
        rungs=tuple(_rung(target, natural.fp_hz, margin) for target in family),
        curve={
            "freqs_hz": [float(value) for value in freqs],
            "median_smoothed_db": [float(v) for v in passband_normalize(freqs, smoothed)],
            "model_db": [float(v) for v in passband_normalize(freqs, model)],
        },
        ceiling_hz=median.ceiling_hz,
        ceiling_source=median.ceiling_source,
        n_positions=median.n_positions,
        level_reference_db=median.level_reference_db,
    )
