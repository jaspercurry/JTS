# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import numpy as np
import pytest

from jasper.audio_measurement.level import LevelReading, solve_gain, stimulus_level

SR = 48_000
BAND = (20.0, 2000.0)
# Four whole cycles per capture period, so a period's phase cannot move its level.
F = 187.5


def _tone(freq_hz: float, rms_db: float, seconds: float = 1.0) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    return np.sqrt(2.0) * 10 ** (rms_db / 20.0) * np.sin(2 * np.pi * freq_hz * t)


def test_a_stimulus_reads_its_own_band_and_the_median_of_its_repeats():
    """Sound outside the band never counts, and a burst in one repeat moves nothing (ADR-0364)."""
    clean = _tone(F, -30.0)
    out_of_band = clean + _tone(5000.0, -10.0)
    burst = clean.copy()
    burst[:4800] += _tone(300.0, -10.0, 0.1)

    reading = stimulus_level([clean, out_of_band, burst], _tone(F, -50.0), gain_db=-20.0, sample_rate=SR,
                             band_hz=BAND)

    assert reading.gain_db == -20.0
    assert reading.level_db == pytest.approx(-30.0, abs=0.2)
    assert reading.floor_db == pytest.approx(-50.0, abs=0.2)


def test_a_stimulus_shorter_than_one_period_reads_nothing():
    assert stimulus_level([_tone(F, -30.0, 0.01)], None, gain_db=-20.0, sample_rate=SR, band_hz=BAND) is None


@pytest.mark.parametrize("floor_db,trusted", [(-41.0, True), (-39.0, False), (None, False)])
def test_a_reading_is_trusted_ten_db_over_its_floor(floor_db, trusted):
    assert LevelReading(-20.0, -30.0, floor_db).trusted is trusted


@pytest.mark.parametrize("level_db,gain", [(68.0, -23.0), (50.0, -19.0), (85.0, -40.0)])
def test_one_solve_steps_1_to_1_to_just_under_the_target(level_db, gain):
    """1 dB under the target, raised at most 15 dB and lowered without a cap (ADR-0364)."""
    assert solve_gain(LevelReading(-34.0, level_db, 40.0), target_db=80.0) == pytest.approx(gain)
