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

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, NamedTuple

import numpy as np

from jasper.audio_measurement.room_limits import spatial_support
from .record_index import bundle_measurements
from .measurement_context import CAPTURE_FIELDS, compare_capture_basis
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

#: The threshold, dB, an RMS delta must exceed before the band is called
#: regressed; a smaller difference is not one. Disclosed in the artifact.
ROOM_GRADE_RESOLUTION_DB = 0.1


@dataclass(frozen=True)
class RoomGradeBand:
    """One band's grade, and the incumbent's beside it when there is one."""

    lo_hz: float
    hi_hz: float
    n_bins: int
    rms_db: float | None
    max_db: float | None
    spread_db: float | None
    compared_hz: tuple[float, float] | None = None
    #: The incumbent's own bin count in this band on ITS grid -- ``None`` with
    #: no incumbent, ``0`` when its grid carries no bin here, which is why the
    #: three numbers below and ``regressed`` read null rather than zero.
    incumbent_n_bins: int | None = None
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
    comparison: Mapping[str, Any] | None = None

    @property
    def regressed_bands(self) -> list[float]:
        return [band.lo_hz for band in self.bands if band.regressed]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": ROOM_GRADE_KIND,
            "resolution_db": ROOM_GRADE_RESOLUTION_DB,
            "ceiling_hz": self.ceiling_hz,
            "ceiling_source": self.ceiling_source,
            "n_positions": self.n_positions,
            "spatial_support": spatial_support(self.n_positions),
            "bands": [asdict(band) for band in self.bands],
            "incumbent": None if self.incumbent_ceiling_hz is None else {
                "ceiling_hz": self.incumbent_ceiling_hz,
                "n_positions": self.incumbent_n_positions,
            },
            "comparison": None if self.comparison is None else dict(self.comparison),
            "regressed_bands": self.regressed_bands,
        }


class _BandMetrics(NamedTuple):
    n_bins: int
    rms_db: float
    max_db: float
    spread_db: float | None


def _array_metrics(
    deviation: np.ndarray, spread: np.ndarray | None, mask: np.ndarray,
) -> _BandMetrics | None:
    values = deviation[mask]
    if not values.size:
        return None
    return _BandMetrics(
        n_bins=int(values.size),
        rms_db=float(np.sqrt(np.mean(values ** 2))),
        max_db=float(np.max(np.abs(values))),
        spread_db=None if spread is None else float(np.mean(spread[mask])),
    )


def _comparison_basis(median: RoomMedian, incumbent: RoomMedian) -> dict[str, Any]:
    def basis(value: RoomMedian) -> dict[str, Any]:
        evidence = value.evidence if isinstance(value.evidence, Mapping) else {}
        raw = evidence.get("basis")
        return {**(raw if isinstance(raw, Mapping) else {}),
                "n_positions": value.n_positions, "pose_keys": evidence.get("pose_keys")}
    return compare_capture_basis(basis(median), basis(incumbent),
                                 required=(*CAPTURE_FIELDS, "n_positions", "pose_keys"))


def _support(median: RoomMedian) -> tuple[float, float]:
    return (
        max(float(median.freqs_hz[0]), float(median.band_hz[0])),
        min(float(median.freqs_hz[-1]), float(median.band_hz[1])),
    )


def _removed_support(
    support: tuple[float, float], common: tuple[float, float] | None,
) -> list[list[float]]:
    if common is None:
        return [[support[0], support[1]]]
    removed = []
    if support[0] < common[0]:
        removed.append([support[0], common[0]])
    if common[1] < support[1]:
        removed.append([common[1], support[1]])
    return removed


def _comparison_arrays(
    median: RoomMedian, incumbent: RoomMedian,
) -> tuple[
    dict[str, Any],
    tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray | None, np.ndarray | None] | None,
]:
    basis = _comparison_basis(median, incumbent)
    now_support, was_support = _support(median), _support(incumbent)
    lo, hi = max(now_support[0], was_support[0]), min(now_support[1], was_support[1])
    common = (lo, hi) if lo < hi else None
    comparison: dict[str, Any] = {
        **basis,
        "available": False,
        "unavailable_reason": None,
        "candidate_support_hz": list(now_support),
        "incumbent_support_hz": list(was_support),
        "common_support_hz": None if common is None else list(common),
        "candidate_removed_support_hz": _removed_support(now_support, common),
        "incumbent_removed_support_hz": _removed_support(was_support, common),
        "level_reference_db": None,
        "level_alignment_db": None,
    }
    if basis["incompatible_fields"]:
        comparison["unavailable_reason"] = "incompatible_measurement_basis"
        return comparison, None
    if common is None:
        comparison["unavailable_reason"] = "no_common_frequency_support"
        return comparison, None

    grid = np.unique(np.concatenate((
        np.asarray([lo, hi]),
        median.freqs_hz[(median.freqs_hz >= lo) & (median.freqs_hz <= hi)],
        incumbent.freqs_hz[(incumbent.freqs_hz >= lo) & (incumbent.freqs_hz <= hi)],
    )))
    now_raw = np.interp(grid, median.freqs_hz, median.median_db + median.level_reference_db)
    was_raw = np.interp(
        grid, incumbent.freqs_hz, incumbent.median_db + incumbent.level_reference_db,
    )
    now_spread, was_spread = (
        None if value.spread_db is None else np.interp(grid, value.freqs_hz, value.spread_db)
        for value in (median, incumbent)
    )
    reference = float(np.median(was_raw))
    alignment = reference - float(np.median(now_raw))
    comparison.update({
        "available": True,
        "level_reference_db": reference,
        "level_alignment_db": alignment,
    })
    return comparison, (
        grid, now_raw + alignment - reference, was_raw - reference,
        now_spread, was_spread,
    )


def grade_room_median(
    median: RoomMedian, *, incumbent: RoomMedian | None = None,
) -> RoomGrade:
    """Grade ``median``, and disclose how each band moved against ``incumbent``.

    With an incumbent, both curves are interpolated onto their common support.
    The incumbent sets the level reference there; one disclosed scalar aligns
    the candidate to it, so a whole-graph attenuation is not tonal regression.
    """
    bands = []
    comparison = None
    compared = None
    if incumbent is not None:
        comparison, compared = _comparison_arrays(median, incumbent)
    for index, (low, high, mask) in enumerate(band_masks(median.freqs_hz, median.ceiling_hz)):
        if incumbent is None or compared is None:
            now = _array_metrics(median.median_db, median.spread_db, mask)
            was = None
            compared_hz = None
        else:
            grid, now_curve, was_curve, now_spread, was_spread = compared
            compared_mask = (grid >= low) & (
                (grid <= high) if index == len(band_masks(grid, median.ceiling_hz)) - 1
                else (grid < high)
            )
            now = _array_metrics(now_curve, now_spread, compared_mask)
            was = _array_metrics(was_curve, was_spread, compared_mask)
            compared_freqs = grid[compared_mask]
            compared_hz = (
                None if not compared_freqs.size
                else (float(compared_freqs[0]), float(compared_freqs[-1]))
            )
        # A band one of the two grids carries nothing in was measured on one
        # side only: there is no difference to state, let alone to grade.
        moved = None if now is None or was is None else now.rms_db - was.rms_db
        bands.append(RoomGradeBand(
            lo_hz=low, hi_hz=high, n_bins=0 if now is None else now.n_bins,
            rms_db=None if now is None else now.rms_db,
            max_db=None if now is None else now.max_db,
            spread_db=None if now is None else now.spread_db,
            compared_hz=compared_hz,
            incumbent_n_bins=(
                None if incumbent is None else 0 if was is None else was.n_bins
            ),
            incumbent_rms_db=None if was is None else was.rms_db,
            incumbent_max_db=None if was is None else was.max_db,
            incumbent_spread_db=None if was is None else was.spread_db,
            delta_rms_db=moved,
            regressed=None if moved is None else moved > ROOM_GRADE_RESOLUTION_DB,
        ))
    return RoomGrade(
        ceiling_hz=median.ceiling_hz,
        ceiling_source=median.ceiling_source,
        n_positions=median.n_positions,
        bands=tuple(bands),
        incumbent_ceiling_hz=None if incumbent is None else incumbent.ceiling_hz,
        incumbent_n_positions=None if incumbent is None else incumbent.n_positions,
        comparison=comparison,
    )


def bundle_graph_scopes(session_dir: Path) -> list[str]:
    """The graph scopes this round's takes played through, for the grade to disclose."""
    return sorted({
        row.graph_scope for row in bundle_measurements(session_dir) if row.graph_scope
    })
