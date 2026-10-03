# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The rear stage's seed, computed from the declared geometry. See ADR-0425."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from jasper.audio_measurement.evidence_reasons import unavailable
from jasper.audio_measurement.measurement_geometry import DeclaredGeometry
from jasper.audio_measurement.null_walk import DEFAULT_SOUND_SPEED_M_S
from jasper.audio_measurement.series_stats import power_mean_db

from .branch_chain import camilla_filter_response
from .crossover_v2.round_captures import ON_AXIS_KEY_PREFIX
from .design_draft import declared_driver_spacing_m
from .rear_calibration import KIND, MAX_CHAIN_BOOST_DB, PHASE_CONVENTION, flat_shelf
from .rear_fit import LOWPASS_ORDER, chain, combo, group_delay_ms

#: The rear's lateness over the woofer spacing at the band centre: a supercardioid.
#: The seat optimum was broad, 0.2 to 0.65 within 0.3 dB (#5438).
SUPERCARDIOID_RATIO = 0.6
#: ADR-0325's complementary Linkwitz-Riley hand-over between the two rear branches.
HANDOVER_ORDER = 4
REAR_SEED_GEOMETRY_UNDECLARED = "rear_seed_geometry_undeclared"
#: The placement the wall distance is derived from (ADR-0317).
PLACEMENT_FIELDS = ("cabinet_back_wall_m", "cabinet_depth_m", "toe_in_degrees")
ASSUMPTIONS = (
    "Computed from the declared geometry: two point sources at the declared woofer spacing (ADR-0425).",
    "The trim is the pair take's level gap at the mark, which both woofers' wall reflections colour.",
)


def rear_seed(sample_rate: int, *, draft: Mapping[str, Any], geometry: DeclaredGeometry | None,
              packet: Mapping[str, Any]) -> dict[str, Any]:
    """The rear stage's starting document, or the gap that names each declaration it lacks.

    ``packet`` is the round's banked packet; its rear pair view levels the rear woofer to the front.
    """
    spacing_m = declared_driver_spacing_m(draft, "rear_woofer_spacing_mm")
    missing = [*(["rear_woofer_spacing_mm"] if spacing_m is None else []),
               *(name for name in PLACEMENT_FIELDS if geometry is None or getattr(geometry, name) is None)]
    if spacing_m is None or geometry is None or missing:
        return unavailable(REAR_SEED_GEOMETRY_UNDECLARED, {"missing": missing})
    speed = DEFAULT_SOUND_SPEED_M_S
    # The front panel's distance to the wall, whose quarter-wave notch is c / (4 * wall).
    wall_m = geometry.boundary_walls()[0]["front"]
    handover_hz = round(speed / (6.0 * wall_m), 4)
    lowpass = combo("ButterworthLowpass", round(speed / (4.0 * spacing_m), 4), LOWPASS_ORDER)
    centre_hz = math.sqrt(handover_hz * lowpass["parameters"]["freq"])
    delay_ms = round(SUPERCARDIOID_RATIO * spacing_m / speed * 1e3 - _group_delay_ms(lowpass, centre_hz), 4)
    trim_db, level = _trim(packet, (handover_hz, lowpass["parameters"]["freq"]))
    return {
        "kind": KIND, "schema": 1, "case": "electrical_dsp", "sample_rate_hz": sample_rate,
        "phase_convention": PHASE_CONVENTION,
        "geometry": {"cabinet_back_wall_m": geometry.cabinet_back_wall_m, "sources": {"front": None, "rear": None},
                     "details": {"rear_woofer_spacing_m": spacing_m, "cabinet_depth_m": geometry.cabinet_depth_m,
                                 "toe_in_degrees": geometry.toe_in_degrees}},
        "reference": {"quantity": "electrical_filter_transfer", "units": "linear output/input", "level": None},
        "conditions": {"trim_db": trim_db, **level},
        "valid_band_hz": None,
        "assumptions": list(ASSUMPTIONS),
        "included_stages": {"front": [], "rear": []},
        "common_delay_ms": max(0.0, -delay_ms),
        "rear_muted": False,
        "front": chain(0.0, 0.0, []),
        "boundary": {"front": [], "rear": []},
        "rear": {
            "mode": "branches",
            "bass": chain(0.0, 0.0, [combo("LinkwitzRileyLowpass", handover_hz, HANDOVER_ORDER), flat_shelf(trim_db)]),
            "cancellation": chain(0.0, delay_ms, [
                combo("LinkwitzRileyHighpass", handover_hz, HANDOVER_ORDER), lowpass, flat_shelf(trim_db),
            ], inverted=True),
        },
    }


def _group_delay_ms(filter_: Mapping[str, Any], freq_hz: float) -> float:
    grid = np.array([freq_hz / 1.001, freq_hz, freq_hz * 1.001])
    return group_delay_ms(camilla_filter_response([filter_], grid), grid, freq_hz)


def _trim(packet: Mapping[str, Any], band_hz: Sequence[float]) -> tuple[float, dict[str, Any]]:
    """Minus the rear-minus-front level the round's pair take read at the mark over the band's
    third octaves, within the boost cap; 0 dB when the round banked no pair view."""
    pairs = [view["pair"] for view in packet.get("rear") or () if view.get("pair")]
    if not pairs:
        return 0.0, {"level_gap_db": None, "pair_round": None}
    rows = [row for pair in pairs for key, position in pair["positions"].items() if key.startswith(ON_AXIS_KEY_PREFIX)
            for row in position["bands"] if band_hz[0] <= math.sqrt(row["band_hz"][0] * row["band_hz"][1]) <= band_hz[1]]
    level: dict[str, Any] = {"level_gap_db": None, "pair_round": packet.get("round_id")}
    if not rows:
        return 0.0, level
    gap_db = round(power_mean_db(np.array([row["rear_db"] for row in rows]))
                   - power_mean_db(np.array([row["front_db"] for row in rows])), 4)
    level["level_gap_db"] = gap_db
    return round(min(-gap_db, MAX_CHAIN_BOOST_DB), 4), level
