# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import numpy as np
import pytest

from jasper.audio_measurement.level import LevelReading, solve_gain, stimulus_level

SR = 48_000
BAND = (20.0, 2000.0)
# Four whole periods per 1024-sample window, so a window's phase cannot move its level.
F = 187.5


def _tone(freq_hz: float, rms_db: float, seconds: float = 1.0) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    return np.sqrt(2.0) * 10 ** (rms_db / 20.0) * np.sin(2 * np.pi * freq_hz * t)


def test_a_stimulus_reads_its_own_band_and_the_median_of_its_repeats():
    """Sound outside the band never counts, and a burst in one repeat moves nothing (ADR-0363)."""
    clean = _tone(F, -30.0)
    out_of_band = clean + _tone(5000.0, -10.0)
    burst = clean.copy()
    burst[:4800] += _tone(300.0, -10.0, 0.1)

    reading = stimulus_level([clean, out_of_band, burst], _tone(F, -50.0), sample_rate=SR, band_hz=BAND)

    assert reading.level_db == pytest.approx(-30.0, abs=0.2)
    assert reading.floor_db == pytest.approx(-50.0, abs=0.2)


@pytest.mark.parametrize("floor_db,trusted", [(-41.0, True), (-39.0, False), (None, False)])
def test_a_reading_is_trusted_ten_db_over_its_floor(floor_db, trusted):
    floor = None if floor_db is None else _tone(F, floor_db)
    reading = stimulus_level([_tone(F, -30.0)], floor, sample_rate=SR, band_hz=BAND)
    assert reading.trusted is trusted


def test_a_stimulus_shorter_than_one_window_reads_nothing():
    assert stimulus_level([_tone(F, -30.0, 0.01)], None, sample_rate=SR, band_hz=BAND) is None


@pytest.mark.parametrize("readings,ceiling,gain,capped,trusted", [
    # One 1:1 step aimed 1 dB under the target, raised at most 15 dB, lowered freely.
    ([(-30.0, LevelReading(66.0, 40.0))], np.inf, -17.0, False, True),
    ([(-30.0, LevelReading(50.0, 30.0))], np.inf, -15.0, False, True),
    ([(-30.0, LevelReading(84.0, 40.0))], np.inf, -35.0, False, True),
    # The ceiling holds it, and says so.
    ([(-30.0, LevelReading(66.0, 40.0))], -20.0, -20.0, True, True),
    # A trusted reading beats a louder one the room inflates.
    ([(-30.0, LevelReading(70.0, 40.0)), (-24.0, LevelReading(78.0, 70.0))], np.inf, -21.0, False, True),
    # With none trusted, the loudest solves: it lands at or under the target.
    ([(-30.0, LevelReading(60.0, 55.0)), (-24.0, LevelReading(64.0, 58.0))], np.inf, -9.0, False, False),
])
def test_one_solve_lands_just_under_the_target(readings, ceiling, gain, capped, trusted):
    solve = solve_gain(readings, target_db=80.0, ceiling_db=ceiling)
    assert (solve.gain_db, solve.capped, solve.trusted) == (pytest.approx(gain), capped, trusted)
    source_gain, source = max(readings, key=lambda pair: (pair[1].trusted, pair[1].level_db))
    assert solve.expected_db == pytest.approx(source.level_db + gain - source_gain)
