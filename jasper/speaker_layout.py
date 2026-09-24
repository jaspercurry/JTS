# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Speaker layout vocabulary: group kinds, main modes and their driver roles,
output variants, the sub crossover corner, measurement target ids, and each
role's declared cone diameter.

Stdlib-only and hardware-free, so the output topology, the active-speaker model
and the tuning programs share one table without importing the DAC or fan-in
bindings.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping

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


def measurement_target_parts(target_id: str) -> tuple[str, str]:
    """``(role, output_variant)`` of a :func:`measurement_target_id`."""
    role, _, variant = target_id.partition(":")
    return role, variant or "primary"


def measurement_target_name(target_id: str) -> str:
    """A :func:`measurement_target_id` in words: ``woofer``, ``rear woofer``."""
    role, variant = measurement_target_parts(target_id)
    return role if variant == "primary" else f"{variant} {role}"


def declared_radiating_diameters_mm(draft: Mapping[str, Any]) -> dict[str, float]:
    """Per-role declared effective radiating diameter, mm (#1665 / #1675), read
    off a design draft's ``manual_settings.drivers``.

    Fail-soft: a role with disagreeing declarations drops entirely, and anything
    malformed is skipped rather than raised. A diameter is a beaming PRIOR, so a
    bad one must cost that one role its prior, never the session.

    Deliberately no default: absent means "not declared", and the receipt says
    so. Substituting a nominal diameter would manufacture a beaming ceiling out
    of nothing, and #1675 is explicit that this is geometry
    guidance derived from a declared dimension.
    """
    manual = draft.get("manual_settings") if isinstance(draft, Mapping) else None
    if not isinstance(manual, Mapping):
        return {}
    drivers = manual.get("drivers")
    out: dict[str, float] = {}
    conflicted: set[str] = set()
    for driver in drivers if isinstance(drivers, list) else []:
        if not isinstance(driver, Mapping):
            continue
        role = str(driver.get("role") or "")
        value = driver.get("radiating_diameter_mm")
        if not role or isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        millimetres = float(value)
        if not math.isfinite(millimetres) or millimetres <= 0.0:
            continue
        if role in out and out[role] != millimetres:
            conflicted.add(role)
            continue
        out[role] = millimetres
    for role in conflicted:
        out.pop(role, None)
    return out


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
