# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""A stimulus's level in its own band, and the one gain solve (ADR-0364, ADR-0365)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np
from scipy.fft import next_fast_len

from jasper.audio_measurement.alignment import _bandlimit
from jasper.audio_measurement.quality import dbfs
from jasper.audio_measurement.ramp import capped_gap_step_db
from jasper.audio_measurement.wired_capture import PERIOD_FRAMES

#: A reading this far over its floor is at most 0.46 dB noise-inflated (ISO 3744's K1).
TRUSTED_OVER_FLOOR_DB = 10.0


@dataclass(frozen=True)
class LevelReading:
    """What a stimulus played at ``gain_db`` read in its loudest period, over the room floor."""

    gain_db: float
    level_db: float
    floor_db: float | None = None

    @property
    def trusted(self) -> bool:
        return self.floor_db is not None and self.level_db - self.floor_db >= TRUSTED_OVER_FLOOR_DB


def level_at_1m_db(level_db: float, distance_m: float) -> float:
    """A far-field reading at ``distance_m`` stated at 1 m by the 1/r law (ADR-0366 §4)."""
    return level_db + 20.0 * math.log10(distance_m)


def _period_mean_squares(samples: np.ndarray, sample_rate: int, band_hz: tuple[float, float]) -> np.ndarray:
    x = np.asarray(samples, dtype=np.float64)
    count = x.size // PERIOD_FRAMES
    if count == 0:
        return np.empty(0)
    # Sweep lengths carry large prime factors, which put an unpadded FFT on its slow path.
    padded = np.pad(x, (0, next_fast_len(x.size, real=True) - x.size))
    filtered = _bandlimit(padded, sample_rate, *band_hz)[:count * PERIOD_FRAMES]
    return np.square(filtered).reshape(count, PERIOD_FRAMES).mean(axis=1)


def stimulus_level(
    stimuli: Sequence[np.ndarray], floor: np.ndarray | None, *, gain_db: float, sample_rate: int,
    band_hz: tuple[float, float],
) -> LevelReading | None:
    """Each stimulus's loudest capture period in ``band_hz``, the median across
    repeats, over the median period of ``floor``. ``None`` when no stimulus spans a period."""
    loudest = [dbfs(math.sqrt(squares.max())) for stimulus in stimuli
               if (squares := _period_mean_squares(stimulus, sample_rate, band_hz)).size]
    if not loudest:
        return None
    floor_squares = _period_mean_squares(floor, sample_rate, band_hz) if floor is not None else np.empty(0)
    return LevelReading(gain_db, float(np.median(loudest)),
                        dbfs(math.sqrt(float(np.median(floor_squares)))) if floor_squares.size else None)


def solve_gain(reading: LevelReading, *, target_db: float, tolerance_db: float, max_raise_db: float) -> float:
    """The gain one 1:1 step from ``reading`` lands half of ``tolerance_db`` under
    ``target_db``, raised at most ``max_raise_db``; the caller clamps it to its own
    ceiling (ADR-0366)."""
    return reading.gain_db + capped_gap_step_db(measured_db=reading.level_db,
                                                target_db=target_db - tolerance_db / 2, cap_db=max_raise_db)
