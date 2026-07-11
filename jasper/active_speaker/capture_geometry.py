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
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping

DRIVER_PLACEMENT_POLICY_ID = "driver_same_distance_v1"
SUMMED_PLACEMENT_POLICY_ID = "summed_listening_position_v1"
COMPARISON_SET_SCHEMA_VERSION = 2
PLACEMENT_PROOF_SCHEMA_VERSION = 1
DRIVER_PLACEMENT_TARGET_CM = 3.0


@dataclass(frozen=True)
class CrossoverLevelReference:
    """One protected driver reference for automatic level match."""

    speaker_group_id: str
    role: str
    tone_frequency_hz: float
    placement_instruction: str

    @property
    def target_id(self) -> str:
        return f"{self.speaker_group_id}:{self.role}"

    @property
    def geometry(self) -> str:
        return f"near_field_driver:{self.target_id}"


def crossover_level_reference(
    preset_payload: Mapping[str, Any],
    *,
    speaker_group_id: str,
    role: str,
) -> CrossoverLevelReference:
    """Resolve one driver's preset-derived, protection-bounded level tone."""

    from .profile import (
        ActiveSpeakerConfigError,
        ActiveSpeakerPreset,
    )
    try:
        preset = ActiveSpeakerPreset.from_mapping(dict(preset_payload))
    except ActiveSpeakerConfigError as exc:
        raise ValueError("applied crossover preset is invalid") from exc

    from .test_signal_plan import driver_test_signal_plan

    group_id = str(speaker_group_id or "").strip()
    role_id = str(role or "").strip().lower()
    if not group_id or not role_id:
        raise ValueError("crossover level target requires a group and driver role")
    plan = driver_test_signal_plan(preset, role_id)
    frequency = plan.get("frequency_hz")
    if plan.get("status") != "ready" or not isinstance(frequency, (int, float)):
        raise ValueError(f"no protected level tone is available for {role_id}")
    return CrossoverLevelReference(
        speaker_group_id=group_id,
        role=role_id,
        tone_frequency_hz=float(frequency),
        placement_instruction=driver_placement_instruction(role_id),
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


_COMPARISON_SET_CORE_KEYS = (
    "schema_version",
    "comparison_set_id",
    "created_at",
    "topology_id",
    "profile_context_id",
    "setup_sha256",
    "device_sha256",
    "calibration_id",
    "driver_level_locks",
)


def comparison_set_fingerprint(value: Mapping[str, Any]) -> str:
    """Fingerprint every immutable comparison-critical field."""

    core = {key: value.get(key) for key in _COMPARISON_SET_CORE_KEYS}
    raw = json.dumps(core, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _driver_level_lock_valid(target_id: Any, value: Any) -> bool:
    if not isinstance(target_id, str) or not target_id or not isinstance(value, Mapping):
        return False
    numeric = (
        tone_frequency := value.get("tone_frequency_hz"),
        value.get("tone_peak_dbfs"),
        value.get("commissioning_gain_db"),
        value.get("locked_main_volume_db"),
    )
    return bool(
        value.get("target_id") == target_id
        and isinstance(value.get("speaker_group_id"), str)
        and value.get("speaker_group_id")
        and isinstance(value.get("role"), str)
        and value.get("role")
        and target_id
        == f"{value.get('speaker_group_id')}:{str(value.get('role')).lower()}"
        and all(
            isinstance(item, (int, float))
            and not isinstance(item, bool)
            and math.isfinite(float(item))
            for item in numeric
        )
        and isinstance(tone_frequency, (int, float))
        and not isinstance(tone_frequency, bool)
        and float(tone_frequency) > 0
    )


def comparison_set_valid(value: Any) -> bool:
    """Whether a schema-v2 per-driver comparison binding is intact."""

    if not isinstance(value, Mapping):
        return False
    locks = value.get("driver_level_locks")
    return bool(
        value.get("schema_version") == COMPARISON_SET_SCHEMA_VERSION
        and isinstance(value.get("comparison_set_id"), str)
        and re.fullmatch(r"[0-9a-f]{32}", value["comparison_set_id"])
        and isinstance(value.get("created_at"), str)
        and value.get("created_at")
        and isinstance(value.get("topology_id"), str)
        and value.get("topology_id")
        and isinstance(value.get("fingerprint"), str)
        and re.fullmatch(r"[0-9a-f]{64}", value["fingerprint"])
        and isinstance(value.get("profile_context_id"), str)
        and value.get("profile_context_id")
        and isinstance(value.get("setup_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", value["setup_sha256"])
        and isinstance(value.get("device_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", value["device_sha256"])
        and isinstance(value.get("calibration_id"), str)
        and isinstance(locks, Mapping)
        and bool(locks)
        and all(_driver_level_lock_valid(key, lock) for key, lock in locks.items())
        and value.get("fingerprint") == comparison_set_fingerprint(value)
    )


def driver_level_lock(
    comparison_set: Mapping[str, Any], speaker_group_id: str, role: str
) -> Mapping[str, Any] | None:
    """Return one verified driver lock from an intact comparison set."""

    if not comparison_set_valid(comparison_set):
        return None
    target_id = f"{speaker_group_id}:{str(role).strip().lower()}"
    value = comparison_set.get("driver_level_locks", {}).get(target_id)
    return value if _driver_level_lock_valid(target_id, value) else None


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
