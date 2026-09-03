# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared acoustic-measurement kernel.

The pure measurement primitives that every JTS tuning layer reuses — room
correction, active-crossover commissioning, and the level ramp — live here
rather than under any one layer's package.

This package imports no feature layer: not :mod:`jasper.correction`, not
:mod:`jasper.active_speaker`. That is what lets both read from it without a
new cross-package edge, and ``tests/test_correction_boundary_ssot.py`` pins
it. Layer-specific logic — PEQ design, targets, correction strategy, the
active-speaker verdicts, the web flows — stays in its owning package.
"""
