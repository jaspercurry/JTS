# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Grade one seat-cube median against flat, band by band, and disclose drift.

The median document is the seat cube's answer: the median deviation from the
room target across the seats, the population spread at each bin, and the
ceiling the room correction was fitted to. Reading it is
:mod:`.room_prescription`'s — the door that computes a prescription's limits
from the same document — so a median this grade accepts is exactly one that
door would prescribe against. This module turns that value into three numbers
per band and, when an incumbent median is handed in beside it, the same three
numbers of the incumbent and the RMS delta between them.

Nothing here decides anything. ``regressed`` is a DISCLOSURE: a measured
regression can restore the incumbent through the doctrine's own path
(docs/measurement-loop-doctrine.md section 3), never through a grade.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, NamedTuple

import numpy as np


from .record_index import bundle_measurements
from .room_prescription import RoomMedian, read_room_median
from .room_views import band_masks

__all__ = [
    "ROOM_GRADE_KIND",
    "ROOM_GRADE_RESOLUTION_DB",
    "RoomGrade",
    "RoomGradeBand",
    "RoomMedian",
    "bundle_graph_scopes",
    "grade_room_median",
    "read_room_median",
]

ROOM_GRADE_KIND = "jts_room_grade"

#: The artifact's numbers are read to this, dB; a band regresses only past it.
ROOM_GRADE_RESOLUTION_DB = 0.1


@dataclass(frozen=True)
class RoomGradeBand:
    """One band's grade, and the incumbent's beside it when there is one."""

    lo_hz: float
    hi_hz: float
    n_bins: int
    rms_db: float
    max_db: float
    spread_db: float
    incumbent_rms_db: float | None = None
    incumbent_max_db: float | None = None
    incumbent_spread_db: float | None = None
    delta_rms_db: float | None = None
    regressed: bool | None = None


@dataclass(frozen=True)
class RoomGrade:
    """The graded bands of one median, and what the incumbent was."""

    ceiling_hz: float
    ceiling_source: str
    n_positions: int
    bands: tuple[RoomGradeBand, ...]
    incumbent_ceiling_hz: float | None = None
    incumbent_n_positions: int | None = None

    @property
    def regressed_bands(self) -> list[float]:
        return [band.lo_hz for band in self.bands if band.regressed]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": ROOM_GRADE_KIND,
            "ceiling_hz": self.ceiling_hz,
            "ceiling_source": self.ceiling_source,
            "n_positions": self.n_positions,
            "bands": [asdict(band) for band in self.bands],
            "incumbent": None if self.incumbent_ceiling_hz is None else {
                "ceiling_hz": self.incumbent_ceiling_hz,
                "n_positions": self.incumbent_n_positions,
            },
            "regressed_bands": self.regressed_bands,
        }




class _BandMetrics(NamedTuple):
    n_bins: int
    rms_db: float
    max_db: float
    spread_db: float


def _metrics(median: RoomMedian, mask: np.ndarray) -> _BandMetrics:
    """One band's bin count, RMS and max against flat, and its mean spread."""
    deviation = median.median_db[mask]
    if not deviation.size:
        return _BandMetrics(0, 0.0, 0.0, 0.0)
    return _BandMetrics(
        n_bins=int(deviation.size),
        rms_db=float(np.sqrt(np.mean(deviation ** 2))),
        max_db=float(np.max(np.abs(deviation))),
        spread_db=float(np.mean(median.spread_db[mask])),
    )


def grade_room_median(
    median: RoomMedian, *, incumbent: RoomMedian | None = None,
) -> RoomGrade:
    """Grade ``median``, and disclose how each band moved against ``incumbent``.

    The incumbent is graded on the CANDIDATE's bands, on its own grid: the two
    documents can carry different ceilings, and the one being graded is the one
    whose bands the reader is looking at.
    """
    bands = []
    incumbent_masks = (
        () if incumbent is None else band_masks(incumbent.freqs_hz, median.ceiling_hz)
    )
    for index, (low, high, mask) in enumerate(band_masks(median.freqs_hz, median.ceiling_hz)):
        now = _metrics(median, mask)
        was = None if incumbent is None else _metrics(incumbent, incumbent_masks[index][2])
        bands.append(RoomGradeBand(
            lo_hz=low, hi_hz=high, n_bins=now.n_bins, rms_db=now.rms_db,
            max_db=now.max_db, spread_db=now.spread_db,
            incumbent_rms_db=None if was is None else was.rms_db,
            incumbent_max_db=None if was is None else was.max_db,
            incumbent_spread_db=None if was is None else was.spread_db,
            delta_rms_db=None if was is None else now.rms_db - was.rms_db,
            regressed=None if was is None else now.rms_db - was.rms_db > ROOM_GRADE_RESOLUTION_DB,
        ))
    return RoomGrade(
        ceiling_hz=median.ceiling_hz,
        ceiling_source=median.ceiling_source,
        n_positions=median.n_positions,
        bands=tuple(bands),
        incumbent_ceiling_hz=None if incumbent is None else incumbent.ceiling_hz,
        incumbent_n_positions=None if incumbent is None else incumbent.n_positions,
    )


def bundle_graph_scopes(session_dir: Path) -> list[str]:
    """The graph scopes this round's takes played through, for the grade to disclose."""
    return sorted({
        row.graph_scope for row in bundle_measurements(session_dir) if row.graph_scope
    })
