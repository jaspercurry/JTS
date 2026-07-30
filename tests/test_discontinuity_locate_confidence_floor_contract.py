# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Drift guard for the sweep-locate-confidence floor (#1839).

``jasper.audio_measurement.program_analysis.DISCONTINUITY_LOCATE_CONFIDENCE_FLOOR``
and ``jasper.active_speaker.crossover_v2_flow.SWEEP_LOCATE_CONFIDENCE_FLOOR``
are the SAME judgment -- "was this MEASURE sweep even heard" -- made
independently at two layers against the identical
``SegmentLocation.confidence`` signal, both filtered to ``KIND_SWEEP``, at
the same threshold, as exact negations of each other:
``_locate_discontinuity`` gates on ``confidence < FLOOR`` (fit refused
below the floor), ``_sweep_locate_confidence_ok`` gates on
``confidence >= FLOOR`` (trusted at or above it).

They are deliberately NOT the same Python object. ``program_analysis`` is
the lower layer ``crossover_v2_flow`` depends on, so ``program_analysis``
must not import from ``crossover_v2_flow`` -- that would invert the
dependency -- and duplicating the constant instead was the #1839 fix. This
test is the seam that keeps the duplication honest: if the flow's floor is
ever deliberately moved (e.g. raised to 0.4 after a bench finding) and
``program_analysis``'s copy is left behind, ``_locate_discontinuity`` would
silently keep fitting steps from sweeps the flow itself no longer trusts --
exactly the fabrication #1839 protects against. A deliberate divergence
must update BOTH constants AND this test. Same pattern as
tests/test_wifi_profile_hardening_contract.py.

Only this test imports both modules -- production code still crosses the
layer boundary in neither direction.
"""
from __future__ import annotations

from jasper.active_speaker.crossover_v2_flow import SWEEP_LOCATE_CONFIDENCE_FLOOR
from jasper.audio_measurement.program_analysis import (
    DISCONTINUITY_LOCATE_CONFIDENCE_FLOOR,
)


def test_discontinuity_and_sweep_locate_confidence_floors_agree():
    assert DISCONTINUITY_LOCATE_CONFIDENCE_FLOOR == SWEEP_LOCATE_CONFIDENCE_FLOOR
