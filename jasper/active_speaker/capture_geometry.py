# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Comparison-critical microphone placement for active-crossover captures.

Per-driver levels are comparable only within the same server-proven microphone
geometry. This module owns that small contract: the placement policies and the
capture copy an operator attests to. That is an attestation, not a measured
distance; near-field and reference-axis locks must never substitute for one
another.
"""

from __future__ import annotations

DRIVER_PLACEMENT_POLICY_ID = "driver_same_distance_v1"
# Deliberately a new policy id: evidence captured under the old
# ``summed_listening_position_v1`` instruction did not bind the microphone to
# the crossover's reference axis or promise that it would remain fixed across
# the normal/reverse pair.  It remains historical evidence, never automatic
# alignment evidence.
SUMMED_PLACEMENT_POLICY_ID = "summed_reference_axis_v1"
REFERENCE_AXIS_DRIVER_PLACEMENT_POLICY_ID = "driver_reference_axis_v1"
DRIVER_PLACEMENT_TARGET_CM = 3.0

# Capture geometry is speaker policy, never browser input.
DRIVER_CAPTURE_GEOMETRY_BY_POLICY = {
    DRIVER_PLACEMENT_POLICY_ID: "near_field",
    REFERENCE_AXIS_DRIVER_PLACEMENT_POLICY_ID: "reference_axis",
}
DRIVER_CAPTURE_GEOMETRIES = frozenset(DRIVER_CAPTURE_GEOMETRY_BY_POLICY.values())


def driver_target_description(role: str) -> str:
    """Return the physical aiming point for a driver role."""

    role = str(role or "driver").strip().lower()
    return {
        "woofer": "centre of the woofer cone",
        "mid": "centre of the midrange cone",
        "tweeter": "centre of the tweeter or horn mouth",
    }.get(role, f"centre of the {role}")


def placement_acknowledgement_label(role: str) -> str:
    """Explicit promise made by the operator before a driver sweep."""

    return (
        f"The microphone capsule is {DRIVER_PLACEMENT_TARGET_CM:g} cm from the "
        f"{driver_target_description(role)} "
        "and I will use this exact distance for every driver measurement."
    )


def reference_axis_driver_acknowledgement_label(role: str) -> str:
    """Explicit stationary-axis promise before an isolated-driver sweep."""

    role = str(role or "driver").strip().lower()
    return (
        "The microphone is on the tweeter axis, level with the centre of the "
        "tweeter or horn mouth, and I will not move it or the speaker while "
        f"measuring the {role} and the other drivers."
    )


def summed_acknowledgement_label() -> str:
    """Explicit promise made before the combined-driver sweep."""

    return (
        "The microphone is on the tweeter axis, level with the centre of the "
        "tweeter or horn mouth, and I will not move it or the speaker between "
        "the normal- and reverse-polarity combined-driver measurements."
    )
