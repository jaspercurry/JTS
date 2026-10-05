# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The design axis, the mark: one test of "is this pose the mark" for both layers that ask.

The kernel's analysis and ``active_speaker``'s contracts share it. The kernel
never imports ``active_speaker``, so it lives here. No numpy:
``crossover_v2.contracts`` re-exports it and stays on the numpy-free import
path ``tests/test_lazy_imports.py`` pins.
"""

from __future__ import annotations

#: The design axis, in ``PositionGeometry``'s own spelling: a capture with no
#: prompted move of its own is a design-axis capture at ``0``. ``None`` is a
#: different fact — "no side was declared" — never a synonym for this.
DESIGN_AXIS_DEG = 0


def on_design_axis(azimuth_deg: float | None, elevation_deg: float | None) -> bool:
    """Whether a pose is the mark, the one pose whose timing a decision reads (ADR-0345, ADR-0433)."""
    return azimuth_deg == DESIGN_AXIS_DEG and elevation_deg == DESIGN_AXIS_DEG
