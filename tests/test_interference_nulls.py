# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Cancellation depth of an inverted branch pair."""
from __future__ import annotations

import math

import pytest

from jasper.audio_measurement.interference_nulls import branch_gap_null_depth_ceiling_db


@pytest.mark.parametrize(
    "gap_db,expected",
    [(0.0, math.inf), (10.0, 3.30), (33.0, 0.20)],
)
def test_the_branch_gap_bounds_the_depth(gap_db, expected):
    """Two branches ``gap`` apart cannot cancel deeper than this however right
    the delay is. 10 dB → ~3.3 dB and 33 dB → ~0.2 dB are the two figures the
    propose door and the composer already state in prose."""
    ceiling = branch_gap_null_depth_ceiling_db(gap_db)
    if math.isinf(expected):
        assert math.isinf(ceiling)
    else:
        assert ceiling == pytest.approx(expected, abs=0.01)
