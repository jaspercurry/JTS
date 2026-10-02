# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pure analysis of a crossover excitation-program capture (design §5.6).

``analyze_program_capture(program, samples, sample_rate) -> ProgramAnalysis``
derives segment locations, per-segment integrity, in-capture clock drift,
per-driver gated responses, tweeter-vs-woofer alignment and the crossover
candidate from the ``(program, capture)`` pair alone. No I/O, no product
policy, and no ``jasper.active_speaker`` import
(``tests/test_audio_measurement_boundary_ssot.py`` pins that boundary), so product
crossover transfers arrive as host-evaluated per-role callables on
:class:`MeasurementPriors`.

The analysis is split across this package by phase. This module lists only the
names product code reads through the package; a test reaches any other name
through the submodule that defines it.
"""

from __future__ import annotations

from jasper.audio_measurement.alignment import parabolic_peak
from .model import (
    ALIGNMENT_OK,
    AppliedAlignment,
    ConfiguredPathConditioningError,
    DriverResponse,
    GainPlan,
    INTEGRITY_CHECK_SWEEP_HEARD,
    IR_POST_MS,
    IR_PRE_MS,
    MeasurementGeometry,
    MeasurementPriors,
    ProgramAnalysis,
)
from .response import (
    deconvolve_window,
    half_period_us,
    polarity_label,
    solve_branch_trims,
)
from .dispatch import analyze_program_capture
from .summary import analysis_diagnostic_summary

__all__ = [
    "ALIGNMENT_OK",
    "AppliedAlignment",
    "ConfiguredPathConditioningError",
    "DriverResponse",
    "GainPlan",
    "INTEGRITY_CHECK_SWEEP_HEARD",
    "IR_POST_MS",
    "IR_PRE_MS",
    "MeasurementGeometry",
    "MeasurementPriors",
    "ProgramAnalysis",
    "analysis_diagnostic_summary",
    "analyze_program_capture",
    "deconvolve_window",
    "half_period_us",
    "parabolic_peak",
    "polarity_label",
    "solve_branch_trims",
]
