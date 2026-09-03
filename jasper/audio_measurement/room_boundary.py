# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""SSOT for the room-correction band ceiling and the gated spec's lower edge.

Homed here rather than in ``correction`` or ``active_speaker`` because those
import each other while ``audio_measurement`` is imported by both and imports
neither (``tests/test_correction_boundary_ssot.py`` pins it). The relation,
stated once: the room ceiling may never be clamped below the gated spec's lower
edge, or a band is left that neither layer owns; between them the two overlap
deliberately (``docs/room-correction-regime-plan.md`` D3). The SNR band tables
in ``acoustic_quality`` / ``snr_policy`` carry a 350 Hz edge that looks like
this boundary and is deliberately NOT routed here.
"""
from __future__ import annotations

import math

# The gated speaker spec's lower edge, Hz — the TABLE's nominal edge, which a
# session intersects with that capture's own trusted floor (2.5/T) before
# grading. Owned here so the room layer's clamp floor and the speaker layer's
# spec floor cannot drift apart; jasper.active_speaker.flat_spec consumes this
# rather than re-declaring 250.
GATED_SPEC_LOWER_EDGE_HZ: float = 250.0

# The room-correction ceiling, Hz, when no per-room estimate is available: the
# shipped Toole-aligned modal/transition boundary.
ROOM_BOUNDARY_DEFAULT_HZ: float = 350.0

# Clamp bounds for a per-room ceiling estimate. The floor is the gated spec's
# lower edge BY DEFINITION (see the module docstring); the ceiling is the widest
# room-correction band the project admits, and is also the `assertive`
# strategy's band.
ROOM_BOUNDARY_MIN_HZ: float = GATED_SPEC_LOWER_EDGE_HZ
ROOM_BOUNDARY_MAX_HZ: float = 500.0


def room_boundary_hz(estimate_hz: float | None = None) -> float:
    """Resolve the room-correction ceiling in Hz, clamped to the bounds above.

    A non-finite or non-positive estimate is treated as *no estimate* and falls
    back to :data:`ROOM_BOUNDARY_DEFAULT_HZ` rather than clamping to a bound, so
    "unknown" never becomes a confident-looking per-room answer.
    """
    if estimate_hz is None:
        return ROOM_BOUNDARY_DEFAULT_HZ
    value = float(estimate_hz)
    if not math.isfinite(value) or value <= 0.0:
        return ROOM_BOUNDARY_DEFAULT_HZ
    return min(max(value, ROOM_BOUNDARY_MIN_HZ), ROOM_BOUNDARY_MAX_HZ)
