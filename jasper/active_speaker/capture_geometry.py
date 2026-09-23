# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Comparison-critical microphone placement for active-crossover captures.

Per-driver levels are comparable only within the same server-proven microphone
geometry. This module owns that small contract for capture copy and durable
evidence. It records an operator attestation, not a measured distance;
near-field and reference-axis locks must never substitute for one another.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

DRIVER_PLACEMENT_POLICY_ID = "driver_same_distance_v1"
# Deliberately a new policy id: evidence captured under the old
# ``summed_listening_position_v1`` instruction did not bind the microphone to
# the crossover's reference axis or promise that it would remain fixed across
# the normal/reverse pair.  It remains historical evidence, never automatic
# alignment evidence.
SUMMED_PLACEMENT_POLICY_ID = "summed_reference_axis_v1"
REFERENCE_AXIS_DRIVER_PLACEMENT_POLICY_ID = "driver_reference_axis_v1"
PLACEMENT_PROOF_SCHEMA_VERSION = 1
DRIVER_PLACEMENT_TARGET_CM = 3.0

# Capture protocol versions carrying the acknowledgement machinery a placement
# proof depends on. Protocol 1 has none, so it is excluded; 2 and 3 authenticate
# the SAME acknowledgement through the SAME
# validate_capture_acknowledgement/on_armed choreography.
#
# **2 stays even though the Pi no longer EMITS it.** This reads a version
# stamped into PERSISTED evidence by whatever page wrote the proof --
# the proof records the PAGE's `capture_protocol_version`,
# and the published build 20260712.3 advertised 2. Dropping 2 here would
# retroactively invalidate every proof captured against that page (repeat
# admission, crossover readiness, replay), so a persisted proof IS a deployed
# artifact even though protocol 2 is no longer emitted.
#
# Explicit allowlist, never a `>=` floor: a future protocol must be a
# deliberate addition here once its acknowledgement choreography is confirmed
# equivalent -- never a silent pass-through.
#
# The literals are duplicated rather than imported on purpose:
# crossover_v2.sweep_spec imports THIS module for its policy ids and
# acknowledgement labels, so importing it back would invert that dependency.
# Containment of its CAPTURE_PROTOCOL_VERSION is pinned by
# tests/test_active_speaker_capture_geometry.py.
PLACEMENT_PROOF_ACKNOWLEDGEMENT_CAPABLE_PROTOCOLS = (2, 3)

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


def placement_proof_shape_valid(
    proof: Mapping[str, Any] | None,
    *,
    policy_id: str,
    role: str,
    speaker_group_id: str,
    target_fingerprint: str,
) -> bool:
    """Whether one placement proof is complete.

    Capture session and acknowledgement identities prove each individual arm,
    but are intentionally not stationary-repeat identity: the product creates
    a fresh capture session for each repeat.
    """

    return bool(
        isinstance(proof, Mapping)
        and isinstance(speaker_group_id, str)
        and bool(speaker_group_id)
        and isinstance(role, str)
        and bool(role)
        and re.fullmatch(r"[0-9a-f]{64}", target_fingerprint)
        and proof.get("schema_version") == PLACEMENT_PROOF_SCHEMA_VERSION
        and proof.get("policy_id") == policy_id
        and proof.get("accepted") is True
        and proof.get("confirmation_source") == "capture_begin"
        and isinstance(proof.get("acknowledgement_binding_sha256"), str)
        and re.fullmatch(
            r"[0-9a-f]{64}",
            proof["acknowledgement_binding_sha256"],
        )
        and isinstance(proof.get("capture_session_id"), str)
        and proof.get("capture_session_id")
        and proof.get("capture_protocol_version")
        in PLACEMENT_PROOF_ACKNOWLEDGEMENT_CAPABLE_PROTOCOLS
        and isinstance(proof.get("capture_page_build"), str)
        and re.fullmatch(r"[0-9]{8}\.[0-9]+", proof["capture_page_build"])
        and proof.get("speaker_group_id") == speaker_group_id
        and proof.get("role") == role
        and proof.get("target_fingerprint") == target_fingerprint
        and isinstance(proof.get("comparison_set_id"), str)
        and re.fullmatch(r"[0-9a-f]{32}", proof["comparison_set_id"])
        and isinstance(proof.get("comparison_set_fingerprint"), str)
        and re.fullmatch(r"[0-9a-f]{64}", proof["comparison_set_fingerprint"])
    )
