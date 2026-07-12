# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Bass-timing adapter for the shared timing-locked null walk."""

from __future__ import annotations

from jasper.audio_measurement.null_walk import (
    MAX_STEP_US,
    NullWalkError,
    NullWalkSpec,
    geometry_seed_us,
)
from jasper.bass_management import active_crossover_corner_hz


def sub_mains_delay_walk_spec(
    *,
    sub_path_minus_mains_m: float,
    transport_delay_ms: float = 0.0,
    step_us: float = MAX_STEP_US,
    corner_hz: float | None = None,
) -> NullWalkSpec:
    """Build the sub-to-mains walk around geometry plus known transport.

    Positive relative delay targets the mains; negative relative delay targets
    the subwoofer. ``sub_path_minus_mains_m`` and the sub's transport latency
    therefore share the required ``negative target minus positive target``
    sign. A lagging wireless sub correctly produces a positive mains delay.

    The speaker-owned corner remains a read from :mod:`jasper.bass_management`;
    active alignment policy lives here rather than widening that fail-soft,
    display-facing resolver. ``corner_hz`` is injectable for orchestration and
    hardware-free tests.
    """

    try:
        fc = active_crossover_corner_hz() if corner_hz is None else float(corner_hz)
        transport_us = float(transport_delay_ms) * 1000.0
    except (TypeError, ValueError) as exc:
        raise NullWalkError("bass delay-walk geometry must be numeric") from exc
    if fc is None:
        raise NullWalkError("bass management has no active crossover corner")
    return NullWalkSpec(
        crossover_fc_hz=fc,
        geometry_seed_us=geometry_seed_us(
            sub_path_minus_mains_m,
            signed_transport_difference_us=transport_us,
        ),
        positive_delay_target="mains",
        negative_delay_target="subwoofer",
        step_us=step_us,
    )
