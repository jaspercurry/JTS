# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The phases a run's takes are planned and banked under (#2291)."""

from __future__ import annotations

PHASE_CHECK = "check"
PHASE_MEASURE = "measure"
PHASE_VERIFY = "verify"
# R16 lateral evidence (plan §4.4): one prompted pose per capture index.
PHASE_LATERAL = "lateral"
# The ADR-0319 timing take: the front drivers summed at the design-axis mark,
# MEASURE's in-session prior.
PHASE_TIMING = "timing"

#: Every phase a take can be planned under, in canonical order.
CAPTURE_PHASES = (
    PHASE_CHECK,
    PHASE_MEASURE,
    PHASE_LATERAL,
    PHASE_TIMING,
    PHASE_VERIFY,
)
