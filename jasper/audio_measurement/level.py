# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""A stimulus's level in its own band (ADR-0364)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np
from scipy.fft import next_fast_len

from jasper.audio_measurement.alignment import _bandlimit
from jasper.audio_measurement.quality import dbfs
from jasper.audio_measurement.wired_capture import PERIOD_FRAMES


@dataclass(frozen=True)
class LevelReading:
    """A loudest-period level and the room floor under it, in one dB reference."""

    level_db: float
    floor_db: float | None = None


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
    stimuli: Sequence[np.ndarray], floor: np.ndarray | None, *, sample_rate: int, band_hz: tuple[float, float],
) -> LevelReading | None:
    """Each stimulus's loudest capture period in ``band_hz``, the median across
    repeats, over the median period of ``floor``. ``None`` when no stimulus spans a period."""
    loudest = [dbfs(math.sqrt(squares.max())) for stimulus in stimuli
               if (squares := _period_mean_squares(stimulus, sample_rate, band_hz)).size]
    if not loudest:
        return None
    floor_squares = _period_mean_squares(floor, sample_rate, band_hz) if floor is not None else np.empty(0)
    return LevelReading(float(np.median(loudest)),
                        dbfs(math.sqrt(float(np.median(floor_squares)))) if floor_squares.size else None)
