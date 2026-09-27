# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

import numpy as np
import pytest

from jasper.audio_measurement.series_stats import local_minima, repeat_spread


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
