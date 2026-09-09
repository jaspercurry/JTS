# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One seat-cube median document, as lane B's ``room_median.json`` carries it.

A fixture library rather than a builder inside a test module, because several
suites read it — the room grade, the bass fit and the exit vocabulary — and a
shared builder living in a collected module makes that module undeletable.

The shape is arithmetic a reader can do: one ripple amplitude per band, one
mode and one dip in the lowest band, and a deviation above the ceiling that no
band may read. The grid and the ceiling satisfy
:func:`~jasper.active_speaker.crossover_v2.room_prescription.read_room_median`
— the door that every reader of this document goes through — so a suite using
this fixture is grading a median the room door would prescribe against.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from jasper.cli.round_views._common import ARTIFACT_BY_VIEW


#: The median's own grid: 1/12 octave from the room floor to the last rung
#: at or below the ceiling, as the producer crops it.
GRID_HZ = 20.0 * 2.0 ** (np.arange(50) / 12.0)

#: Inside the door's ``ROOM_BOUNDARY_MIN_HZ``-``ROOM_BOUNDARY_MAX_HZ`` window.
CEILING_HZ = 350.0
BAND_EDGES_HZ = ((20.0, 60.0), (60.0, 120.0), (120.0, CEILING_HZ))
#: Bins per band on :data:`GRID_HZ`.
BAND_BINS = (20, 12, 18)
MODE_HZ, MODE_DB = 33.0, 6.0
DIP_HZ, DIP_DB = 45.0, -8.0
RIPPLE_DB = (2.0, 2.0, 2.0)
SPREAD_DB = 1.5
N_POSITIONS = 7
#: The incumbent: worse in the lowest band, better in the middle one, equal
#: above — one regressed band each way.
INCUMBENT: dict[str, Any] = {
    "ripple_db": (2.0, 1.0, 2.0), "mode_db": 9.0, "dip_db": -10.0,
}


def median_document(
    freqs_hz: Sequence[float],
    median_db: Sequence[float],
    *,
    ceiling_hz: float = CEILING_HZ,
    ceiling_source: str = "applied_candidate",
) -> dict[str, Any]:
    """That document around ANY curve, for a suite whose median is its own.

    The deviations are a fixed fan about the median rather than a model of
    seat-to-seat spread: no reader of this fixture grades them.
    """
    bins = len(freqs_hz)
    fan = np.where(np.arange(bins) % 2 == 0, 1.0, -1.0)
    return {
        "freqs_hz": list(freqs_hz),
        "median_db": list(median_db),
        "spread_db": [SPREAD_DB] * bins,
        "n_positions": N_POSITIONS,
        "positions": [
            {"id": f"seat-{index}", "deviation_db": (fan * index).tolist()}
            for index in range(N_POSITIONS)
        ],
        "ceiling_hz": ceiling_hz,
        "ceiling_source": ceiling_source,
        "window": "ungated",
    }


def room_median_document(
    *,
    ceiling_hz: float = CEILING_HZ,
    ceiling_source: str = "applied_candidate",
    ripple_db: Sequence[float] = RIPPLE_DB,
    mode_db: float = MODE_DB,
    dip_db: float = DIP_DB,
) -> dict[str, Any]:
    """One seat cube's median, as lane B's ``room_median.json`` carries it."""
    grid = GRID_HZ[GRID_HZ <= ceiling_hz]
    alternating = np.where(np.arange(grid.size) % 2 == 0, 1.0, -1.0)
    median_db = np.zeros(grid.size)
    for (low, high), ripple in zip(BAND_EDGES_HZ, ripple_db):
        in_band = (grid >= low) & (grid <= high)
        median_db[in_band] = ripple * alternating[in_band]
    median_db[int(np.argmin(np.abs(grid - MODE_HZ)))] = mode_db
    median_db[int(np.argmin(np.abs(grid - DIP_HZ)))] = dip_db
    return median_document(
        grid.tolist(), median_db.tolist(),
        ceiling_hz=ceiling_hz, ceiling_source=ceiling_source,
    )


def write_room_median(round_dir: Path, **kwargs: Any) -> Path:
    """That document where a view of a BANKED round reads it: beside the round."""
    path = Path(round_dir) / ARTIFACT_BY_VIEW["room-median"].artifact
    path.write_text(json.dumps(room_median_document(**kwargs)))
    return path
