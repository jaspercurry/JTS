# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Room response and taper facts shared by judge and preview."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from jasper.active_speaker.branch_chain import chain_response
from jasper.audio_measurement.room_boundary import ROOM_FLOOR_HZ
from jasper.audio_measurement.room_limits import boost_cap_db, cut_floor_db, spatial_support
from jasper.audio_measurement.seat_figures import spread_rms_db

from .blend_prescription import composed_grid

@dataclass(frozen=True, eq=False)
class RoomMedian:
    """The room's spatial median and the measured basis for its bounds.

    ``deviations_db`` is positions x bins, relative to ``median_db``.
    ``level_reference_db`` is the producer curve's median level over the band.
    """

    freqs_hz: np.ndarray
    median_db: np.ndarray
    spread_db: np.ndarray | None
    deviations_db: np.ndarray
    n_positions: int
    ceiling_hz: float
    ceiling_source: str
    level_reference_db: float = 0.0
    evidence: Mapping[str, Any] | None = None
    coverage_hz: tuple[float, float] | None = None

    @property
    def band_hz(self) -> tuple[float, float]:
        return self.coverage_hz or (ROOM_FLOOR_HZ, self.ceiling_hz)


def _response_db(entries: Sequence[Mapping[str, Any]], grid: np.ndarray) -> np.ndarray:
    response = 20.0 * np.log10(np.maximum(np.abs(chain_response(entries, grid)), 1e-12))
    if not np.all(np.isfinite(response)):
        raise ValueError("room response is not finite")
    return response


@dataclass(frozen=True)
class RoomComposition:
    freqs_hz: np.ndarray
    cut_floor_db: np.ndarray
    boost_cap_db: np.ndarray
    composed_db: Mapping[str, np.ndarray]

    def violations(self, sides: Mapping[str, Sequence[Mapping[str, Any]]], tolerance_db: float) -> list[dict[str, Any]]:
        bins = []
        for side, composed in self.composed_db.items():
            indices = np.flatnonzero(np.maximum(
                self.cut_floor_db - composed, composed - self.boost_cap_db,
            ) > tolerance_db)
            contributions = [_response_db([entry], self.freqs_hz[indices]) for entry in sides[side]]
            for offset, index in enumerate(indices):
                bins.append({
                    "side": side, "freq_hz": float(self.freqs_hz[index]),
                    "composed_db": float(composed[index]),
                    "cut_floor_db": float(self.cut_floor_db[index]),
                    "boost_cap_db": float(self.boost_cap_db[index]),
                    "filters": [{"index": position, **entry, "response_db": float(response[offset])}
                                for position, (entry, response) in enumerate(zip(sides[side], contributions))],
                })
        return bins

    def preview(self, median: RoomMedian, tolerance_db: float) -> dict[str, Any]:
        residual = {side: median.median_db + np.interp(median.freqs_hz, self.freqs_hz, composed)
                    for side, composed in self.composed_db.items()}
        return {
            "freqs_hz": self.freqs_hz.tolist(),
            "cut_floor_db": self.cut_floor_db.tolist(), "boost_cap_db": self.boost_cap_db.tolist(),
            "composed_tolerance_db": tolerance_db,
            "sides": {side: {
                "composed_db": composed.tolist(),
                # Positive margins remain inside the taper; tolerance is separate.
                "cut_margin_db": (composed - self.cut_floor_db).tolist(),
                "boost_margin_db": (self.boost_cap_db - composed).tolist(),
            } for side, composed in self.composed_db.items()},
            "residual": {
                "freqs_hz": median.freqs_hz.tolist(), "level_reference_db": median.level_reference_db,
                "sides": {side: curve.tolist() for side, curve in residual.items()},
            },
            "summary": _residual_summary(median, residual),
        }


def _residual_summary(median: RoomMedian, residual: Mapping[str, np.ndarray]) -> dict[str, Any]:
    """The playbook's test over the fitted band: each side's predicted median
    residual against the seat spread that sets the cut floor, both as the room
    view's ``spread_rms_db`` reduces them."""
    band_hz = [median.band_hz[0], median.ceiling_hz]
    spread = spread_rms_db(median.spread_db, median.freqs_hz, band_hz=band_hz)
    sides = {}
    for side, curve in residual.items():
        rms = spread_rms_db(curve, median.freqs_hz, band_hz=band_hz)
        sides[side] = {"residual_rms_db": rms,
                       "under_seat_spread": None if rms is None or spread is None else rms < spread}
    return {"band_hz": band_hz, "spatial_support": spatial_support(median.n_positions),
            "seat_spread_rms_db": spread, "sides": sides}


def room_composition(sides: Mapping[str, Sequence[Mapping[str, Any]]], median: RoomMedian,
                     floor_db: np.ndarray) -> RoomComposition:
    band = (median.band_hz[0], median.ceiling_hz)
    grid = np.unique(np.concatenate((composed_grid(band, median.freqs_hz), median.freqs_hz, band)))
    floor = np.maximum(
        np.interp(grid, median.freqs_hz, floor_db),
        cut_floor_db(None if median.spread_db is None else 0.0, grid, median.ceiling_hz),
    )
    return RoomComposition(grid, floor, boost_cap_db(grid, median.ceiling_hz),
                           {side: _response_db(entries, grid) for side, entries in sides.items()})
