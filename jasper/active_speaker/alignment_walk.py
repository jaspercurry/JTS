# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Active-driver adapter for the shared timing-locked null walk.

The adapter builds the spec and declares the active-crossover scope. There is
no shared runner: ``null_walk`` is decision content only, so any host that
executes a walk owns its own DSP mutation, writer exclusion, exact restore, and
lifecycle events. :mod:`jasper.audio_measurement.delay_graph` is what validates
the declared scope today.
"""

from __future__ import annotations

from typing import Any

from jasper.audio_measurement.null_walk import (
    DelayWalkScope,
    MAX_STEP_US,
    NullWalkSpec,
    geometry_seed_us,
)

DRIVER_DELAY_WALK_SCOPE: DelayWalkScope = "active_crossover"


def driver_delay_walk_spec(
    *,
    crossover_fc_hz: Any,
    positive_delay_target_role: str,
    negative_delay_target_role: str,
    signed_acoustic_path_difference_m: Any,
    step_us: Any = MAX_STEP_US,
) -> NullWalkSpec:
    """Bound one driver-to-driver walk from an a-priori geometry estimate.

    ``signed_acoustic_path_difference_m`` is negative-target path length minus
    positive-target path length. The shared spec maps either sign to a
    non-negative delay plus the executable driver-role target.
    """

    return NullWalkSpec(
        crossover_fc_hz=crossover_fc_hz,
        geometry_seed_us=geometry_seed_us(signed_acoustic_path_difference_m),
        positive_delay_target=positive_delay_target_role,
        negative_delay_target=negative_delay_target_role,
        step_us=step_us,
    )
