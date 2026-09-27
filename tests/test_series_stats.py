# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

import numpy as np
import pytest

from jasper.audio_measurement.series_stats import repeat_spread


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
