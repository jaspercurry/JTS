# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Level, tilt, flatness and difference statistics for measured response curves."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np

from .band_ladders import SERIES_STATS_FLATNESS_BAND_HZ, SERIES_STATS_TILT_BAND_HZ
from .spatial_combine import octave_bands_hz


def power_mean_db(values_db: np.ndarray) -> float:
    """``10*log10(mean(10**(dB/10)))`` — power (energy) mean, NOT a linear
    average of dB values."""
    linear = np.power(10.0, values_db / 10.0)
    return float(10.0 * np.log10(np.mean(linear)))


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

    With ``remove_level`` each curve's median over the band comes off before
    subtracting, and the offset is published: two graphs, or a model with no
    absolute reference, differ by a level that is not a difference in shape.
    ``None`` when ``curve_db`` has no bin in the band.
    """
    freqs = np.asarray(freqs_hz, dtype=float)
    mask = (freqs >= band_hz[0]) & (freqs <= band_hz[1])
    if not np.any(mask):
        return None
    curve = np.asarray(curve_db, dtype=float)[mask]
    against = np.interp(freqs[mask], np.asarray(against_freqs_hz, dtype=float), np.asarray(against_db, dtype=float))
    offset = float(np.median(curve) - np.median(against)) if remove_level else 0.0
    return CurveDifference(freqs[mask], curve, against, offset)


def series_stats(
    curve: Mapping[str, Any], plot: Mapping[str, Any], trusted_floor_hz: float | None,
) -> dict[str, Any]:
    def number(value: float | None, lo_hz: float) -> dict[str, Any]:
        return {"value": value, "below_trusted_floor": value is not None
                and trusted_floor_hz is not None and lo_hz < trusted_floor_hz}

    freqs = np.asarray(plot["freqs_hz"], dtype=float)
    values = np.asarray(plot["deviation_db"], dtype=float)
    valid = np.isfinite(values) & (freqs > 0)
    tilt_lo_hz = max(SERIES_STATS_TILT_BAND_HZ[0], trusted_floor_hz or SERIES_STATS_TILT_BAND_HZ[0])
    measured = valid & (freqs >= tilt_lo_hz) & (freqs <= SERIES_STATS_TILT_BAND_HZ[1])
    raw_freqs = np.asarray(curve["freqs_hz"], dtype=float)
    raw = np.asarray(curve["display"]["deviation_db"], dtype=float)
    flatness_lo_hz = trusted_floor_hz if trusted_floor_hz is not None else SERIES_STATS_FLATNESS_BAND_HZ[0]
    flatness_band = np.isfinite(raw) & (raw_freqs >= flatness_lo_hz) & (raw_freqs <= SERIES_STATS_FLATNESS_BAND_HZ[1])
    centered = raw[flatness_band] - np.mean(raw[flatness_band]) if flatness_band.any() else raw[flatness_band]
    bands = {}
    for center, lo, hi in octave_bands_hz(20, 20000):
        band = values[valid & (freqs >= lo) & (freqs < hi)]
        bands[f"{center:g}"] = number(power_mean_db(band) if band.size else None, lo)
    return {
        "tilt_db_per_decade": number(float(np.polyfit(np.log10(freqs[measured]), values[measured], 1)[0])
                                     if np.unique(freqs[measured]).size >= 2 else None, tilt_lo_hz),
        "flatness_rms_db": {
            "value": float(np.sqrt(np.mean(centered ** 2))) if centered.size else None,
            "band_hz": [flatness_lo_hz, SERIES_STATS_FLATNESS_BAND_HZ[1]],
        },
        "band_means_db": bands,
        "low_end_means_db": {f"{b['band_hz'][0]}_{b['band_hz'][1]}": number(b["mean_db"], b["band_hz"][0])
                             for b in plot["band_means"]},
    }
