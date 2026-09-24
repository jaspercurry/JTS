# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""A stimulus's level in its own band, and the one gain solve (ADR-0363)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np

from jasper.audio_measurement.alignment import _bandlimit
from jasper.audio_measurement.quality import dbfs

#: One capture period, about 21 ms at 48 kHz: the window the SPL stop judges.
WINDOW_FRAMES = 1024
#: A reading this far over its floor is at most 0.46 dB noise-inflated (ISO 3744 K1).
TRUSTED_OVER_FLOOR_DB = 10.0
#: A solve lands this far under its target, inside the ±2 dB band.
AIM_UNDER_TARGET_DB = 1.0
#: The most one solve raises a gain (ADR-0361).
MAX_RAISE_DB = 15.0


@dataclass(frozen=True)
class LevelReading:
    """A loudest-window level and the room floor under it, in one dB reference."""

    level_db: float
    floor_db: float | None = None

    @property
    def trusted(self) -> bool:
        return self.floor_db is not None and self.level_db - self.floor_db >= TRUSTED_OVER_FLOOR_DB


@dataclass(frozen=True)
class LevelSolve:
    gain_db: float
    expected_db: float
    capped: bool
    trusted: bool


def _window_levels_db(samples: np.ndarray, sample_rate: int, band_hz: tuple[float, float]) -> np.ndarray:
    x = np.asarray(samples, dtype=np.float64)
    count = x.size // WINDOW_FRAMES
    if count == 0:
        return np.empty(0)
    filtered = _bandlimit(x, sample_rate, *band_hz)[:count * WINDOW_FRAMES]
    mean_squares = np.square(filtered).reshape(count, WINDOW_FRAMES).mean(axis=1)
    return np.array([dbfs(math.sqrt(value)) for value in mean_squares])


def stimulus_level(
    stimuli: Sequence[np.ndarray], floor: np.ndarray | None, *, sample_rate: int, band_hz: tuple[float, float],
) -> LevelReading | None:
    """Each stimulus's loudest window in ``band_hz``, the median across repeats,
    over the median window of ``floor``. ``None`` when no stimulus spans a window."""
    loudest = [levels.max() for stimulus in stimuli
               if (levels := _window_levels_db(stimulus, sample_rate, band_hz)).size]
    if not loudest:
        return None
    floor_levels = _window_levels_db(floor, sample_rate, band_hz) if floor is not None else np.empty(0)
    return LevelReading(float(np.median(loudest)), float(np.median(floor_levels)) if floor_levels.size else None)


def solve_gain(
    readings: Sequence[tuple[float, LevelReading]], *, target_db: float, ceiling_db: float = math.inf,
) -> LevelSolve:
    """One 1:1 step from ``(gain, reading)`` pairs to just under ``target_db``, never above ``ceiling_db``.

    The loudest trusted reading wins. With none trusted the loudest reading is
    noise-inflated, so its solve lands at or under the target, never over it.
    """
    gain, reading = max(readings, key=lambda pair: (pair[1].trusted, pair[1].level_db))
    wanted = gain + min(target_db - AIM_UNDER_TARGET_DB - reading.level_db, MAX_RAISE_DB)
    solved = min(wanted, ceiling_db)
    return LevelSolve(solved, reading.level_db + solved - gain, capped=solved < wanted, trusted=reading.trusted)
