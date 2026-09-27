# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

import numpy as np
import pytest

from jasper.audio_measurement.series_stats import flatness, local_minima, repeat_spread


@pytest.mark.parametrize("repeats,expected", [
    ([], None),
    ([-50.0], None),
    ([-50.0, -51.0], 1.0),
    ([100.0, 101.0, 150.0], 50.0),
    ([[0.0, 1.0], [2.0, 1.0], [1.0, 4.0]], [2.0, 3.0]),
])
def test_the_repeat_spread_is_the_range_across_repeats(repeats, expected):
    spread = repeat_spread(repeats)
    assert spread is None if expected is None else np.asarray(spread).tolist() == pytest.approx(expected)


@pytest.mark.parametrize("curve,band_hz,expected", [
    pytest.param([0, -1, -3, -1, 0, 0], (3.0, 5.0), [2], id="inside"),
    pytest.param([0, -1, -3, -1, 0, 0], (3.0, 4.0), [2], id="on-the-bottom-edge"),
    pytest.param([0, -1, -3, -1, 0, 0], (2.0, 3.0), [2], id="on-the-top-edge"),
    pytest.param([0, -1, -3, -1, 0, 0], (3.0, 3.0), [2], id="a-one-bin-band"),
    pytest.param([0, -1, -2, -3, -4, -5], (2.0, 4.0), [], id="a-slope-out-of-the-band"),
    pytest.param([-5, -4, -3, -2, -1, 0], (1.0, 3.0), [], id="the-curve-end"),
    pytest.param([0, -2, -2, 0, 0, 0], (1.0, 6.0), [2], id="one-bin-of-a-flat-bottom"),
])
def test_a_dip_is_a_local_minimum_of_the_whole_curve_read_in_the_closed_band(curve, band_hz, expected):
    freqs = np.arange(1.0, 7.0)
    assert local_minima(freqs, np.asarray(curve, dtype=float), band_hz).tolist() == expected


@pytest.mark.parametrize("curve,band_hz,expected", [
    # About the mean the same bins read sqrt(0.75) = 0.87 dB.
    pytest.param([9, 0, 0, 2, 0, 9], (2.0, 5.0), (0.0, 4, 1.0), id="about-the-median"),
    pytest.param([9, 0, 0, 2, 0, 9], (1.0, 6.0), (1.0, 6, None), id="closed-band"),
    pytest.param([9, 0, np.nan, 2, 0, 9], (2.0, 5.0), (0.0, 3, None), id="finite-bins-only"),
    pytest.param([9, 0, 0, 2, 0, 9], (2.5, 2.9), None, id="no-bin"),
])
def test_flatness_is_the_rms_about_the_median_of_the_closed_band(curve, band_hz, expected):
    flat = flatness(np.arange(1.0, 7.0), curve, band_hz)
    if expected is None:
        assert flat is None
        return
    median_db, bins, rms_db = expected
    assert (flat["median_db"], flat["bins"]) == (median_db, bins)
    assert rms_db is None or flat["rms_db"] == pytest.approx(rms_db)
