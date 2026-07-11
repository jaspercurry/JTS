# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Comparison-critical microphone placement for active-crossover captures.

Per-driver levels are comparable only when every capture uses the same
near-field geometry.  This module owns that small contract for the relay copy,
the durable measurement record, the sequential envelope, and the baseline
compiler.  It records an operator attestation, not a measured distance.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping

DRIVER_PLACEMENT_POLICY_ID = "driver_same_distance_v1"
SUMMED_PLACEMENT_POLICY_ID = "summed_listening_position_v1"
COMPARISON_SET_SCHEMA_VERSION = 1
PLACEMENT_PROOF_SCHEMA_VERSION = 1
DRIVER_PLACEMENT_TARGET_CM = 3.0
CROSSOVER_LEVEL_TONE_FREQUENCY_HZ = 1000.0


@dataclass(frozen=True)
class CrossoverLevelReference:
    """One bound reference role/frequency/placement for automatic level match."""

    role: str
    tone_frequency_hz: float
    placement_instruction: str


def crossover_level_reference(
    preset_payload: Mapping[str, Any],
    *,
    tone_frequency_hz: float = CROSSOVER_LEVEL_TONE_FREQUENCY_HZ,
) -> CrossoverLevelReference:
    """Resolve the driver whose protected passband carries the level tone.

    The full crossover graph plays during level matching. The microphone must
    therefore be aimed at the radiator whose preset-derived acoustic band
    contains that exact tone. This chooses the woofer for an ordinary 2-way
    1 kHz reference and the midrange for the supported 3-way shape; it fails
    closed if the applied preset cannot prove that relationship.
    """

    from .profile import (
        ActiveSpeakerConfigError,
        ActiveSpeakerPreset,
        crossover_edges_for_role,
        required_driver_roles,
    )

    try:
        frequency = float(tone_frequency_hz)
    except (TypeError, ValueError) as exc:
        raise ValueError("crossover level tone frequency is invalid") from exc
    if not math.isfinite(frequency) or frequency <= 0:
        raise ValueError("crossover level tone frequency is invalid")
    try:
        preset = ActiveSpeakerPreset.from_mapping(dict(preset_payload))
    except ActiveSpeakerConfigError as exc:
        raise ValueError("applied crossover preset is invalid") from exc

    for role in required_driver_roles(preset.way_count):
        lower_hz, upper_hz = crossover_edges_for_role(preset, role)
        if lower_hz is not None and frequency < lower_hz:
            continue
        if upper_hz is not None and frequency > upper_hz:
            continue
        return CrossoverLevelReference(
            role=role,
            tone_frequency_hz=frequency,
            placement_instruction=driver_placement_instruction(role),
        )
    raise ValueError(
        "applied crossover has no driver passband containing the level tone"
    )


def driver_target_description(role: str) -> str:
    """Return the physical aiming point for a driver role."""

    role = str(role or "driver").strip().lower()
    return {
        "woofer": "centre of the woofer cone",
        "mid": "centre of the midrange cone",
        "tweeter": "centre of the tweeter or horn mouth",
    }.get(role, f"centre of the {role}")


def driver_placement_instruction(role: str) -> str:
    """One canonical household instruction for a comparable capture."""

    target = driver_target_description(role)
    return (
        f"Move the microphone capsule to {DRIVER_PLACEMENT_TARGET_CM:g} cm "
        f"(about 1¼ in) from the {target}, "
        "pointed straight at it. Use this same distance for every driver."
    )


def summed_placement_instruction() -> str:
    """Canonical placement for the combined-driver validation."""

    return (
        "Move the microphone to the main listening position at ear height, "
        "pointed toward the speaker. This is a new position after the "
        "near-field driver measurements."
    )


def placement_acknowledgement_label(role: str) -> str:
    """Explicit promise made by the operator before a driver sweep."""

    return (
        f"The microphone capsule is {DRIVER_PLACEMENT_TARGET_CM:g} cm from the "
        f"{driver_target_description(role)} "
        "and I will use this exact distance for every driver measurement."
    )


def summed_acknowledgement_label() -> str:
    """Explicit promise made before the combined-driver sweep."""

    return (
        "I moved the microphone to the main listening position at ear height "
        "for the combined-driver measurement."
    )


def comparison_set_valid(value: Any) -> bool:
    """Whether a persisted comparison-set binding has the complete v1 shape."""

    if not isinstance(value, Mapping):
        return False
    locked = value.get("locked_main_volume_db")
    return bool(
        value.get("schema_version") == COMPARISON_SET_SCHEMA_VERSION
        and isinstance(value.get("comparison_set_id"), str)
        and re.fullmatch(r"[0-9a-f]{32}", value["comparison_set_id"])
        and isinstance(value.get("fingerprint"), str)
        and re.fullmatch(r"[0-9a-f]{64}", value["fingerprint"])
        and isinstance(value.get("profile_context_id"), str)
        and value.get("profile_context_id")
        and isinstance(value.get("setup_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", value["setup_sha256"])
        and isinstance(value.get("device_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", value["device_sha256"])
        and isinstance(value.get("calibration_id"), str)
        and isinstance(locked, (int, float))
        and not isinstance(locked, bool)
        and math.isfinite(float(locked))
    )


def normalized_placement_proof(
    *,
    policy_id: str,
    acknowledgement_binding: str,
    relay_session_id: str,
    capture_page: Mapping[str, Any] | None,
    speaker_group_id: str,
    role: str,
    target_fingerprint: str,
    comparison_set: Mapping[str, Any],
) -> dict[str, Any]:
    """Create the server-owned proof persisted after a verified relay arm."""

    if not comparison_set_valid(comparison_set):
        raise ValueError("active crossover comparison set is invalid")
    page = capture_page if isinstance(capture_page, Mapping) else {}
    return {
        "schema_version": PLACEMENT_PROOF_SCHEMA_VERSION,
        "policy_id": policy_id,
        "accepted": True,
        "confirmation_source": "relay_begin_capture",
        "acknowledgement_binding_sha256": hashlib.sha256(
            acknowledgement_binding.encode("utf-8")
        ).hexdigest(),
        "relay_session_id": relay_session_id,
        "capture_protocol_version": page.get("capture_protocol_version"),
        "capture_page_build": page.get("capture_page_build"),
        "speaker_group_id": speaker_group_id,
        "role": role,
        "target_fingerprint": target_fingerprint,
        "comparison_set_id": comparison_set["comparison_set_id"],
        "comparison_set_fingerprint": comparison_set["fingerprint"],
    }


def capture_proof_valid(
    record: Mapping[str, Any] | None,
    active_comparison_set: Mapping[str, Any] | None,
    *,
    policy_id: str,
    role: str,
    speaker_group_id: str,
    target_fingerprint: str = "",
) -> bool:
    """Whether one acoustic record belongs to the active comparable set."""

    if (
        not isinstance(record, Mapping)
        or not isinstance(active_comparison_set, Mapping)
        or not comparison_set_valid(active_comparison_set)
    ):
        return False
    proof = record.get("placement_proof")
    if not isinstance(proof, Mapping):
        return False
    expected_target = target_fingerprint or str(
        record.get("target_fingerprint") or ""
    )
    return bool(
        proof.get("schema_version") == PLACEMENT_PROOF_SCHEMA_VERSION
        and proof.get("policy_id") == policy_id
        and proof.get("accepted") is True
        and proof.get("confirmation_source") == "relay_begin_capture"
        and isinstance(proof.get("acknowledgement_binding_sha256"), str)
        and re.fullmatch(
            r"[0-9a-f]{64}",
            proof["acknowledgement_binding_sha256"],
        )
        and isinstance(proof.get("relay_session_id"), str)
        and proof.get("relay_session_id")
        and proof.get("capture_protocol_version") == 2
        and isinstance(proof.get("capture_page_build"), str)
        and re.fullmatch(r"[0-9]{8}\.[0-9]+", proof["capture_page_build"])
        and proof.get("speaker_group_id") == speaker_group_id
        and proof.get("role") == role
        and proof.get("target_fingerprint", "") == expected_target
        and proof.get("comparison_set_id")
        == active_comparison_set.get("comparison_set_id")
        and proof.get("comparison_set_fingerprint")
        == active_comparison_set.get("fingerprint")
    )
