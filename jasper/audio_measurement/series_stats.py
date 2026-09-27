# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Level, tilt, flatness, difference and repeat statistics for measured response curves."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import mean, stdev
from typing import Any, Mapping, Sequence

import numpy as np

from .band_ladders import SERIES_STATS_FLATNESS_BAND_HZ, SERIES_STATS_TILT_BAND_HZ
from .spatial_combine import octave_bands_hz


def power_mean_db(values_db: np.ndarray) -> float:
    """``10*log10(mean(10**(dB/10)))`` — power (energy) mean, NOT a linear
    average of dB values."""
    linear = np.power(10.0, values_db / 10.0)
    return float(10.0 * np.log10(np.mean(linear)))


def power_mean_across_db(stack_db: np.ndarray) -> np.ndarray:
    """Per-column power mean across the rows of a dB matrix: curves pooled to
    a curve, where :func:`power_mean_db` pools one curve to a scalar."""
    return 10.0 * np.log10(np.mean(np.power(10.0, stack_db / 10.0), axis=0))


def deviation_summary(freqs_hz: np.ndarray, deviation_db: np.ndarray) -> dict[str, Any]:
    """One deviation curve as scalars, with the frequency its worst bin sits at."""
    worst = int(np.argmax(np.abs(deviation_db)))
    return {
        "bins": int(deviation_db.size),
        "mean_abs_db": float(np.mean(np.abs(deviation_db))),
        "max_abs_db": float(abs(deviation_db[worst])),
        "max_abs_hz": float(freqs_hz[worst]),
        "rms_db": float(np.sqrt(np.mean(deviation_db**2))),
    }


def band_bins(freqs_hz: Any, band_hz: Sequence[float]) -> np.ndarray:
    """The bins one band reads: the closed band, both edges included (#5661)."""
    freqs = np.asarray(freqs_hz, dtype=float)
    return (freqs >= float(band_hz[0])) & (freqs <= float(band_hz[1]))


def local_minima(freqs_hz: Any, curve_db: Any, band_hz: Sequence[float]) -> np.ndarray:
    """The dips of ``curve_db`` in ``band_hz``: its bins that sit at or below
    the bin before and below the bin after, read on the whole curve. A
    neighbour outside the band is measured data, so a dip on the band's edge
    counts and a slope running out of the band does not; the curve's first
    and last bins have one neighbour and never count. ``<=`` then ``<`` keeps
    one bin of a flat bottom. Indices into the curve, ascending."""
    curve = np.asarray(curve_db, dtype=float)
    inside = np.flatnonzero(band_bins(freqs_hz, band_hz))
    inside = inside[(inside > 0) & (inside < curve.size - 1)]
    return inside[(curve[inside] <= curve[inside - 1]) & (curve[inside] < curve[inside + 1])]


def flatness(freqs_hz: Any, curve_db: Any, band_hz: Sequence[float]) -> dict[str, Any] | None:
    """How flat ``curve_db`` is over the closed band: its finite bins'
    departure from the level most of them agree on, their median (ADR-0358's
    level rule, read on one curve), as :func:`deviation_summary`'s scalars
    beside that ``median_db``. Read at the curve's own smoothing. ``None``
    with no finite bin there. See #5661."""
    freqs = np.asarray(freqs_hz, dtype=float)
    curve = np.asarray(curve_db, dtype=float)
    inside = band_bins(freqs, band_hz) & np.isfinite(curve)
    if not inside.any():
        return None
    median_db = float(np.median(curve[inside]))
    return {"median_db": median_db, **deviation_summary(freqs[inside], curve[inside] - median_db)}


@dataclass(frozen=True)
class CurveDifference:
    """``curve_db - against_db`` on the curve's grid, after ``level_offset_db``
    came off the curve."""

    freqs_hz: np.ndarray
    curve_db: np.ndarray
    against_db: np.ndarray
    level_offset_db: float

    @property
    def delta_db(self) -> np.ndarray:
        return (self.curve_db - self.level_offset_db) - self.against_db


def curve_difference(
    freqs_hz: np.ndarray, curve_db: np.ndarray, against_freqs_hz: np.ndarray, against_db: np.ndarray, *,
    band_hz: tuple[float, float], remove_level: bool = True,
) -> CurveDifference | None:
    """``curve_db`` minus ``against_db`` over ``band_hz``, the second read onto the first's grid.

    With ``remove_level`` the level most bins agree on, the median of the
    per-bin difference over the band, comes off before subtracting, and the
    offset is published: two graphs, or a model with no absolute reference,
    differ by a level that is not a difference in shape, and a filter that
    reshapes a minority of bins does not move it (ADR-0358). A band's change
    is the same median over that band.
    ``None`` when ``curve_db`` has no bin in the band.
    """
    freqs = np.asarray(freqs_hz, dtype=float)
    mask = band_bins(freqs, band_hz)
    if not np.any(mask):
        return None
    curve = np.asarray(curve_db, dtype=float)[mask]
    against = np.interp(freqs[mask], np.asarray(against_freqs_hz, dtype=float), np.asarray(against_db, dtype=float))
    offset = float(np.median(curve - against)) if remove_level else 0.0
    return CurveDifference(freqs[mask], curve, against, offset)


def series_stats(plot: Mapping[str, Any], trusted_floor_hz: float | None) -> dict[str, Any]:
    def number(value: float | None, lo_hz: float) -> dict[str, Any]:
        return {"value": value, "below_trusted_floor": value is not None
                and trusted_floor_hz is not None and lo_hz < trusted_floor_hz}

    freqs = np.asarray(plot["freqs_hz"], dtype=float)
    values = np.asarray(plot["deviation_db"], dtype=float)
    valid = np.isfinite(values) & (freqs > 0)
    tilt_lo_hz = max(SERIES_STATS_TILT_BAND_HZ[0], trusted_floor_hz or SERIES_STATS_TILT_BAND_HZ[0])
    measured = valid & (freqs >= tilt_lo_hz) & (freqs <= SERIES_STATS_TILT_BAND_HZ[1])
    flatness_lo_hz = trusted_floor_hz if trusted_floor_hz is not None else SERIES_STATS_FLATNESS_BAND_HZ[0]
    flat = flatness(freqs, values, (flatness_lo_hz, SERIES_STATS_FLATNESS_BAND_HZ[1]))
    bands = {}
    for center, lo, hi in octave_bands_hz(20, 20000):
        band = values[valid & (freqs >= lo) & (freqs < hi)]
        bands[f"{center:g}"] = number(power_mean_db(band) if band.size else None, lo)
    return {
        "tilt_db_per_decade": number(float(np.polyfit(np.log10(freqs[measured]), values[measured], 1)[0])
                                     if np.unique(freqs[measured]).size >= 2 else None, tilt_lo_hz),
        "flatness_rms_db": {
            "value": None if flat is None else flat["rms_db"],
            "band_hz": [flatness_lo_hz, SERIES_STATS_FLATNESS_BAND_HZ[1]],
        },
        "band_means_db": bands,
        "low_end_means_db": {f"{b['band_hz'][0]}_{b['band_hz'][1]}": number(b["mean_db"], b["band_hz"][0])
                             for b in plot["band_means"]},
    }


def band_change_db(
    freqs_hz: np.ndarray, curve_db: np.ndarray, against_db: np.ndarray, band_hz: tuple[float, float],
) -> float | None:
    """How far ``curve_db`` sits from ``against_db`` over ``band_hz``, both on
    ``freqs_hz``: :func:`curve_difference`'s level rule. ``None`` with no bin there."""
    difference = curve_difference(freqs_hz, curve_db, freqs_hz, against_db, band_hz=band_hz)
    return None if difference is None else difference.level_offset_db


def repeat_spread(repeats: Any) -> Any:
    """The repeat spread: the range, max minus min, of one reading across
    repeats of one condition, along the first axis. A float for readings, one
    value per bin for a stack of curves; ``None`` below two repeats, where no
    spread was measured. See ADR-0319 and ADR-0325."""
    stack = np.asarray(repeats, dtype=float)
    if stack.ndim == 0 or stack.shape[0] < 2:
        return None
    spread = np.ptp(stack, axis=0)
    return float(spread) if spread.ndim == 0 else spread


def sample_spread(values: Sequence[float]) -> dict[str, float] | None:
    if len(values) < 2:
        return None
    return {"n": float(len(values)), "mean": mean(values), "sd": stdev(values),
            "range": repeat_spread(values), "min": min(values), "max": max(values)}
