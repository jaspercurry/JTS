# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The one test of "is this pose the mark", shared by the kernel's analysis and
``active_speaker``'s contracts."""

from __future__ import annotations

import math

import pytest

from jasper.active_speaker.crossover_v2 import contracts
from jasper.audio_measurement import design_axis


@pytest.mark.parametrize(
    "azimuth_deg, elevation_deg, on_mark",
    [
        (0, 0, True),
        (0.0, 0.0, True),
        (-0.0, -0.0, True),
        (7, 0, False),
        (-7, 0, False),
        (0, 7, False),
        (0, -7, False),
        (7, 7, False),
        # Exact, never toleranced: a pose a hair off the mark is not the mark.
        (1e-9, 0, False),
        (0, -1e-9, False),
        (math.nan, 0, False),
        # `None` is "no side declared", a different fact from 0 degrees.
        (None, 0, False),
        (0, None, False),
        (None, None, False),
    ],
)
def test_only_zero_azimuth_at_zero_elevation_is_the_mark(azimuth_deg, elevation_deg, on_mark):
    assert design_axis.on_design_axis(azimuth_deg, elevation_deg) is on_mark


def test_contracts_re_exports_the_kernel_predicate_rather_than_a_second_copy():
    assert contracts.on_design_axis is design_axis.on_design_axis
    assert contracts.DESIGN_AXIS_DEG == design_axis.DESIGN_AXIS_DEG
