# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Where this speaker may be crossed, and what its preset becomes there.

The corner is executed, not hunted: nothing here ranks corners. Only the two
drivers' declared hard excitation bands may refuse one — never a bound that
cannot name the damage mechanism it protects (#2870). The name is historical:
R17's corner sweep was deleted (``docs/tuning-master-plan.md`` ruling R1);
no sweep remains.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

__all__ = [
    "FC_REJECT_ABOVE_LOWER_DRIVER_BAND",
    "FC_REJECT_BELOW_DECLARED_FLOOR",
    "recornered_preset",
]

# Owner ruling 2026-08-17: a corner exactly AT the declared minimum recommended
# crossover is a sanctioned operating point, so only strictly below is refused.
FC_REJECT_BELOW_DECLARED_FLOOR = "below_declared_floor"
FC_REJECT_ABOVE_LOWER_DRIVER_BAND = "above_lower_driver_band"


def _fc_rejection(
    fc_hz: float,
    hf_hard_floor_hz: float,
    lower_driver_hard_ceiling_hz: float,
) -> str | None:
    """The FIRST bound ``fc_hz`` violates, hardest first, or ``None``."""
    if fc_hz < float(hf_hard_floor_hz):
        return FC_REJECT_BELOW_DECLARED_FLOOR
    if fc_hz > float(lower_driver_hard_ceiling_hz):
        return FC_REJECT_ABOVE_LOWER_DRIVER_BAND
    return None


def recornered_preset(preset: Any, *, fc_hz: float, order: int | None = None) -> Any:
    """``preset`` with every crossover region moved to ``fc_hz`` (and ``order``).

    The region ``id`` spelling is a contract with
    ``staging.compile_preset_from_crossover_preview``, which recompiles it as
    ``f"{lower_role}_{upper_role}_{int(round(frequency))}hz"``; any other
    spelling — a pinned order joining the name included — is refused
    ``measured_candidate_preset_mismatch`` at apply. Change this format only
    together with staging's.
    """
    moved: dict[str, Any] = {"fc_hz": float(fc_hz)}
    if order is not None:
        moved["order"] = int(order)
    return replace(preset, crossover_regions=tuple(
        replace(
            region,
            id=(
                f"{region.lower_driver}_{region.upper_driver}"
                f"_{int(round(float(fc_hz)))}hz"
            ),
            **moved,
        )
        for region in preset.crossover_regions
    ))
