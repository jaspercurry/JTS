# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Speaker layout vocabulary: group kinds, main modes and their driver roles,
output variants, the sub crossover corner, and measurement target ids.

Stdlib-only and hardware-free, so the output topology, the active-speaker model
and the tuning programs share one table without importing the DAC or fan-in
bindings.
"""

from __future__ import annotations

from typing import Iterable

SUPPORTED_GROUP_KINDS = {"left", "right", "mono", "subwoofer"}
MAIN_GROUP_KINDS = frozenset(SUPPORTED_GROUP_KINDS) - {"subwoofer"}
PASSIVE_MAIN_MODE = "full_range_passive"
# Roles run low to high: the first has no lower crossover edge, consecutive
# roles cross over, and a way-count view assumes one mode per way count.
MAIN_DRIVER_ROLES_BY_MODE = {
    PASSIVE_MAIN_MODE: ("full_range",),
    "active_2_way": ("woofer", "tweeter"),
    "active_3_way": ("woofer", "mid", "tweeter"),
}
WAY_COUNT_BY_MAIN_MODE = {mode: len(roles) for mode, roles in MAIN_DRIVER_ROLES_BY_MODE.items()}
ADJACENT_PAIRS_BY_MAIN_MODE = {
    mode: tuple(zip(roles, roles[1:])) for mode, roles in MAIN_DRIVER_ROLES_BY_MODE.items()
}
LOWEST_DRIVER_ROLE_BY_MAIN_MODE = {mode: roles[0] for mode, roles in MAIN_DRIVER_ROLES_BY_MODE.items()}
REQUIRED_ROLES_BY_MODE = {**MAIN_DRIVER_ROLES_BY_MODE, "subwoofer": ("subwoofer",)}
SUPPORTED_GROUP_MODES = set(REQUIRED_ROLES_BY_MODE)
SUPPORTED_ROLES = {
    role for roles in REQUIRED_ROLES_BY_MODE.values() for role in roles
}

OUTPUT_VARIANT_SCHEMA_VERSION = 2
SUPPORTED_OUTPUT_VARIANTS = {"primary", "rear"}

# The local-DAC sub's bass-management crossover corner (Hz) and its legal
# bounds (jasper.active_speaker.LocalSubwoofer): the sub output LR4 low-passes
# here and the mains' lowest driver LR4 high-passes at the SAME corner. The
# 200 Hz ceiling is load-bearing safety: `graph_safety.sub_audible_guard_present`
# caps an audible sub's low-pass at it, so a corner ceiling that drifted higher
# than the guard's would let a wider-than-legal sub low-pass past the guard.
# LR4 (order 4) is the standard sub/main slope; both halves at order 4.
DEFAULT_SUB_CROSSOVER_HZ = 80.0
SUB_CROSSOVER_HZ_LO = 40.0
SUB_CROSSOVER_HZ_HI = 200.0
SUB_CROSSOVER_ORDER = 4


def measurement_target_id(role: str, output_variant: str = "primary") -> str:
    """One physical driver output's identity inside a speaker group.

    A primary output's id IS its role, so every role-keyed measurement map on a
    primary-only speaker is unchanged; a rear woofer adds ``woofer:rear``
    (ADR-0316). :func:`physical_target_id` is the same id under its group.
    """
    return role if output_variant == "primary" else f"{role}:{output_variant}"


def cardioid_cabinet_channels(
    outputs: Iterable[tuple[str, str, int]],
) -> tuple[int, int, int] | None:
    """``(front woofer, rear woofer, tweeter)`` channel indexes of the one
    cabinet a rear calibration document describes, or ``None``.

    ADR-0318: exactly one rear output, one front output of the rear's role, and
    one output of the other role, over ``(role, variant, index)`` triples. The
    caller owns what else its own topology must satisfy.
    """
    items = list(outputs)
    rear = [item for item in items if item[1] == "rear"]
    if len(rear) != 1:
        return None
    role = rear[0][0]
    front = [item for item in items if item[1] != "rear" and item[0] == role]
    tweeter = [item for item in items if item[0] != role]
    if len(front) != 1 or len(tweeter) != 1:
        return None
    return front[0][2], rear[0][2], tweeter[0][2]


def physical_target_id(group_id: str, role: str, output_variant: str = "primary") -> str:
    return f"{group_id}:{measurement_target_id(role, output_variant)}"
