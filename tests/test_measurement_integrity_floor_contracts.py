# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Drift guard for the two capture-integrity floors duplicated across layers.

``jasper.audio_measurement.program_analysis`` and
``jasper.active_speaker.crossover_v2_flow`` each declare
``SWEEP_LOCATE_CONFIDENCE_FLOOR`` and ``SWEEP_SCHEDULE_RESIDUAL_CEILING_MS``.
Each pair is ONE judgment about a located sweep -- "was it even heard" and
"did it land on its scheduled slot" -- made independently at two layers
against the identical ``SegmentLocation`` signal, at the identical threshold.

What each copy governs, so a reader can see the duplication is real reuse and
not an accident:

* the FLOW's copies gate MEASURE's ``KIND_SWEEP`` segments --
  ``_sweep_locate_confidence_ok`` on ``confidence >= FLOOR``,
  ``_sweep_schedule_ok`` on ``residual_ms <= CEILING``;
* ``program_analysis``'s copies gate VERIFY's single ``KIND_SUMMED_SWEEP``
  (``_verify_capture_integrity``), a kind neither flow gate filters for, and
  the confidence floor additionally refuses ``_locate_discontinuity``'s step
  fit below it (``confidence < FLOOR``, the exact negation).

They are deliberately NOT the same Python object. ``program_analysis`` is the
lower layer ``crossover_v2_flow`` depends on, so ``program_analysis`` must not
import from ``crossover_v2_flow`` -- that would invert the dependency -- and
duplicating instead was the #1839 fix (confidence floor) and the #1971 fix
(residual ceiling). This test is the seam that keeps the duplication honest:
if a floor is deliberately moved (e.g. raised after a bench finding) and the
other copy is left behind, the two layers silently disagree about which
captures they trust -- ``_locate_discontinuity`` would keep fitting steps from
sweeps the flow no longer trusts (#1839's fabrication), and a VERIFY capture
would be graded against a different splice bound than a MEASURE one. A
deliberate divergence must update BOTH constants AND this test. Same pattern
as tests/test_wifi_profile_hardening_contract.py.

Only this test imports both modules -- production code still crosses the layer
boundary in neither direction.
"""
from __future__ import annotations

from jasper.active_speaker.crossover_v2_flow import (
    SWEEP_LOCATE_CONFIDENCE_FLOOR as FLOW_LOCATE_CONFIDENCE_FLOOR,
)
from jasper.active_speaker.crossover_v2_flow import (
    SWEEP_SCHEDULE_RESIDUAL_CEILING_MS as FLOW_SCHEDULE_RESIDUAL_CEILING_MS,
)
from jasper.audio_measurement.program_analysis import (
    SWEEP_LOCATE_CONFIDENCE_FLOOR as ANALYSIS_LOCATE_CONFIDENCE_FLOOR,
)
from jasper.audio_measurement.program_analysis import (
    SWEEP_SCHEDULE_RESIDUAL_CEILING_MS as ANALYSIS_SCHEDULE_RESIDUAL_CEILING_MS,
)


def test_locate_confidence_floors_agree():
    assert ANALYSIS_LOCATE_CONFIDENCE_FLOOR == FLOW_LOCATE_CONFIDENCE_FLOOR


def test_schedule_residual_ceilings_agree():
    assert ANALYSIS_SCHEDULE_RESIDUAL_CEILING_MS == FLOW_SCHEDULE_RESIDUAL_CEILING_MS
