# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Bound one reverse-null delay walk, and state the bars it is read against.

Decision content only: it plays nothing, mutates no graph, and applies no
delay. The method of record is compute-then-confirm
(:mod:`jasper.active_speaker.crossover_v2.delay_landscape`), which is what
consumes both the spec and the depth bars below.

The grid, its scoring and its selection stay in
:mod:`jasper.audio_measurement.null_walk`; the null-depth number is
:func:`jasper.audio_measurement.analysis.crossover_null_depth_db`'s.
"""

from __future__ import annotations

from typing import Any

from jasper.active_speaker.alignment_walk import driver_delay_walk_spec
from jasper.audio_measurement.null_walk import NullWalkSpec

# Both bars are docs/tuning-methodology.md's, and both are depths in dB re the
# Fc/2 and 2*Fc shoulder mean — the one null-depth definition
# `analyze_summed_crossover` already emits. They answer a different question
# from `driver_acoustics.DEFAULT_NULL_THRESHOLD_DB` (6.0), which asks whether a
# null formed AT ALL for the polarity call; these ask whether the null that
# formed is deep enough to trust the DELAY it implies.
ROBUST_NULL_DEPTH_DB = 20.0
# When NO coordinate the sweep measured reaches this, the residual at Fc is not
# a timing residual at all — it is axis or lobing. Disclosed as a verdict,
# never raised as an error.
USABLE_NULL_DEPTH_DB = 15.0

VERDICT_ROBUST = "delay_resolved_robust"
VERDICT_WEAK = "delay_resolved_weak"
VERDICT_AXIS_LIMITED = "axis_or_lobing_limited"
def sweep_spec(
    *,
    crossover_fc_hz: float,
    upper_role: str,
    lower_role: str,
    signed_acoustic_path_difference_m: float,
    step_us: float | None = None,
) -> NullWalkSpec:
    """Bound one sweep from the crossover corner and the declared geometry.

    Every bound is derived, never per-speaker: the grid spans one half period at
    ``crossover_fc_hz`` either side of the geometry seed, because beyond that a
    reverse null repeats into the next cycle and the deepest point stops being
    unique. ``signed_acoustic_path_difference_m`` is lower-driver path minus
    upper-driver path; pass 0.0 when the geometry is undeclared, which centres
    the same half-period window on zero.
    """

    kwargs: dict[str, Any] = {
        "crossover_fc_hz": crossover_fc_hz,
        "positive_delay_target_role": upper_role,
        "negative_delay_target_role": lower_role,
        "signed_acoustic_path_difference_m": signed_acoustic_path_difference_m,
    }
    if step_us is not None:
        kwargs["step_us"] = step_us
    return driver_delay_walk_spec(**kwargs)


__all__ = [
    "ROBUST_NULL_DEPTH_DB",
    "USABLE_NULL_DEPTH_DB",
    "VERDICT_AXIS_LIMITED",
    "VERDICT_ROBUST",
    "VERDICT_WEAK",
    "sweep_spec",
]
