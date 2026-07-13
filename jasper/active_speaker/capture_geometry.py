# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Comparison-critical microphone placement for active-crossover captures.

Per-driver levels are comparable only within the same server-proven microphone
geometry. This module owns that small contract for relay copy, durable evidence,
and level-lock identity. It records an operator attestation, not a measured
distance; near-field and reference-axis locks must never substitute for one
another.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping

DRIVER_PLACEMENT_POLICY_ID = "driver_same_distance_v1"
# Deliberately a new policy id: evidence captured under the old
# ``summed_listening_position_v1`` instruction did not bind the microphone to
# the crossover's reference axis or promise that it would remain fixed across
# the normal/reverse pair.  It remains historical evidence, never automatic
# alignment evidence.
SUMMED_PLACEMENT_POLICY_ID = "summed_reference_axis_v1"
REFERENCE_AXIS_DRIVER_PLACEMENT_POLICY_ID = "driver_reference_axis_v1"
COMPARISON_SET_SCHEMA_VERSION = 2
PLACEMENT_PROOF_SCHEMA_VERSION = 1
DRIVER_PLACEMENT_TARGET_CM = 3.0

# Capture geometry is speaker policy, never browser input. The relay verifies
# one of these policy ids before playback and persists it in placement_proof;
# analysis derives the DSP geometry from that server-owned proof. Lane B's
# fixed-axis driver capture can therefore enter the same repeat/ambient/
# excitation/persistence path as today's near-field capture.
DRIVER_CAPTURE_GEOMETRY_BY_POLICY = {
    DRIVER_PLACEMENT_POLICY_ID: "near_field",
    REFERENCE_AXIS_DRIVER_PLACEMENT_POLICY_ID: "reference_axis",
}
DRIVER_CAPTURE_GEOMETRIES = frozenset(DRIVER_CAPTURE_GEOMETRY_BY_POLICY.values())
SUMMED_CAPTURE_GEOMETRY_BY_POLICY = {
    SUMMED_PLACEMENT_POLICY_ID: "reference_axis",
}


def driver_repeat_binding(
    *,
    speaker_group_id: str,
    role: str,
    target_fingerprint: str,
    capture_geometry: str,
) -> tuple[str, str]:
    """Return one geometry-scoped repeat-admission identity.

    The physical topology fingerprint remains the placement-proof identity.
    Fixed-axis attempts get a derived controller identity so they can never
    continue or complete the near-field repeat set for the same driver.
    """

    group_id = str(speaker_group_id or "").strip()
    role_id = str(role or "").strip().lower()
    fingerprint = str(target_fingerprint or "").strip()
    geometry = str(capture_geometry or "").strip().lower()
    if not group_id or role_id not in _active_crossover_driver_roles():
        raise ValueError("driver repeat binding requires a valid group and role")
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ValueError("driver repeat binding requires a target fingerprint")
    if geometry not in DRIVER_CAPTURE_GEOMETRIES:
        raise ValueError("driver repeat binding has unsupported capture geometry")
    target_id = f"{group_id}:{role_id}"
    if geometry == "near_field":
        return target_id, fingerprint
    repeat_fingerprint = hashlib.sha256(
        json.dumps(
            {
                "capture_geometry": geometry,
                "target_fingerprint": fingerprint,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return f"reference_axis:{target_id}", repeat_fingerprint


def _active_crossover_driver_roles() -> frozenset[str]:
    """Return the canonical 2/3-way role vocabulary without an import cycle."""

    from .profile import DRIVER_ROLES_BY_WAY

    return frozenset(
        role
        for way_count in (2, 3)
        for role in DRIVER_ROLES_BY_WAY[way_count]
    )


def driver_level_geometry(
    speaker_group_id: str,
    role: str,
    capture_geometry: str,
) -> str:
    """Stable level-lock key for one physical driver and mic geometry."""

    group_id = str(speaker_group_id or "").strip()
    role_id = str(role or "").strip().lower()
    geometry = str(capture_geometry or "").strip().lower()
    if not group_id or not role_id:
        raise ValueError("driver level geometry requires a group and role")
    if role_id not in _active_crossover_driver_roles():
        raise ValueError("driver level geometry has unsupported driver role")
    if geometry not in DRIVER_CAPTURE_GEOMETRIES:
        raise ValueError("driver level geometry is unsupported")
    return f"{geometry}_driver:{group_id}:{role_id}"


def parse_driver_level_geometry(value: str) -> tuple[str, str, str]:
    """Parse one canonical ``geometry_driver:group:role`` level-lock key.

    Group ids may legally contain ``:``. Parse the role from the right only
    after matching a known geometry prefix, then require the canonical writer
    to reproduce the byte-exact input. This rejects whitespace/mixed-case
    aliases and prevents a malformed string from selecting a larger level cap.
    """

    if not isinstance(value, str) or not value:
        raise ValueError("driver level geometry is empty")
    capture_geometry = next(
        (
            geometry
            for geometry in DRIVER_CAPTURE_GEOMETRIES
            if value.startswith(f"{geometry}_driver:")
        ),
        None,
    )
    if capture_geometry is None:
        raise ValueError("driver level geometry has unsupported geometry")
    remainder = value.removeprefix(f"{capture_geometry}_driver:")
    speaker_group_id, separator, role = remainder.rpartition(":")
    if not separator or not speaker_group_id or not role:
        raise ValueError("driver level geometry requires a group and role")
    if role not in _active_crossover_driver_roles():
        raise ValueError("driver level geometry has unsupported driver role")
    if (
        driver_level_geometry(
            speaker_group_id,
            role,
            capture_geometry,
        )
        != value
    ):
        raise ValueError("driver level geometry is not canonical")
    return capture_geometry, speaker_group_id, role


def _capture_geometry_from_proof(
    placement_proof: Mapping[str, Any],
    active_comparison_set: Mapping[str, Any] | None,
    *,
    geometry_by_policy: Mapping[str, str],
    speaker_group_id: str,
    role: str,
    target_fingerprint: str,
    capture_kind: str,
) -> str:
    """Resolve geometry only after re-proving the authoritative context."""

    policy_id = placement_proof.get("policy_id")
    try:
        geometry = geometry_by_policy[str(policy_id)]
    except KeyError as exc:
        raise ValueError(
            f"{capture_kind} capture placement policy is unsupported"
        ) from exc
    record = {
        "placement_proof": placement_proof,
        "target_fingerprint": target_fingerprint,
    }
    if not capture_proof_valid(
        record,
        active_comparison_set,
        policy_id=str(policy_id),
        role=role,
        speaker_group_id=speaker_group_id,
        target_fingerprint=target_fingerprint,
    ):
        raise ValueError(
            f"{capture_kind} capture placement proof is invalid or stale"
        )
    return geometry


def driver_capture_geometry(
    placement_proof: Mapping[str, Any] | None,
    active_comparison_set: Mapping[str, Any] | None = None,
    *,
    speaker_group_id: str = "",
    role: str = "",
    target_fingerprint: str = "",
) -> str:
    """Resolve driver analysis geometry from server-owned placement proof.

    Missing/legacy proof remains near-field so operator-only historical paths
    preserve their behavior. A fixed-reference-axis relay must carry the
    explicit reference-axis policy; no request field can opt into gating.
    Unknown policies fail closed rather than silently selecting a geometry.
    """

    if not isinstance(placement_proof, Mapping):
        return "near_field"
    return _capture_geometry_from_proof(
        placement_proof,
        active_comparison_set,
        geometry_by_policy=DRIVER_CAPTURE_GEOMETRY_BY_POLICY,
        speaker_group_id=speaker_group_id,
        role=role,
        target_fingerprint=target_fingerprint,
        capture_kind="driver",
    )


def summed_capture_geometry(
    placement_proof: Mapping[str, Any] | None,
    active_comparison_set: Mapping[str, Any] | None = None,
    *,
    speaker_group_id: str = "",
    target_fingerprint: str = "",
) -> str:
    """Resolve summed analysis geometry from a complete fixed-axis proof.

    Missing historical/operator-only proof remains near-field and therefore
    cannot enter the automatic alignment decision boundary. A proved relay
    capture can only resolve to the fixed reference axis.
    """

    if not isinstance(placement_proof, Mapping):
        return "near_field"
    return _capture_geometry_from_proof(
        placement_proof,
        active_comparison_set,
        geometry_by_policy=SUMMED_CAPTURE_GEOMETRY_BY_POLICY,
        speaker_group_id=speaker_group_id,
        role="summed",
        target_fingerprint=target_fingerprint,
        capture_kind="summed",
    )


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
        return driver_level_geometry(
            self.speaker_group_id, self.role, "near_field"
        )


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


def reference_axis_driver_placement_instruction(role: str) -> str:
    """Canonical stationary axis shared by each isolated-driver capture."""

    role = str(role or "driver").strip().lower()
    return (
        "Place the microphone capsule on the tweeter axis, exactly level with "
        "the centre of the tweeter or horn mouth, about 1 metre away when the "
        "room permits. Aim it according to its calibration file. Keep the "
        f"microphone and speaker completely still while measuring the {role} "
        "and every other driver in this set."
    )


def summed_placement_instruction() -> str:
    """Canonical fixed-axis placement for combined-driver alignment evidence."""

    return (
        "Place the microphone capsule on the tweeter axis, exactly level with "
        "the centre of the tweeter or horn mouth, about 1 metre away when the "
        "room permits. Aim it according to its calibration file, then keep the "
        "microphone and speaker completely still for every normal- and "
        "reverse-polarity combined-driver capture in this measurement set."
    )


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
        placement_proof_shape_valid(
            proof,
            policy_id=policy_id,
            role=role,
            speaker_group_id=speaker_group_id,
            target_fingerprint=expected_target,
        )
        and proof.get("comparison_set_id")
        == active_comparison_set.get("comparison_set_id")
        and proof.get("comparison_set_fingerprint")
        == active_comparison_set.get("fingerprint")
    )


def placement_proof_shape_valid(
    proof: Mapping[str, Any] | None,
    *,
    policy_id: str,
    role: str,
    speaker_group_id: str,
    target_fingerprint: str,
) -> bool:
    """Whether one proof is complete before authoritative-set comparison.

    Relay session and acknowledgement identities prove each individual arm,
    but are intentionally not stationary-repeat identity: the product creates
    a fresh relay link for each repeat. Comparison/target/group/role are the
    cross-repeat binding and are checked separately by the aggregator.
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
        and proof.get("target_fingerprint") == target_fingerprint
        and isinstance(proof.get("comparison_set_id"), str)
        and re.fullmatch(r"[0-9a-f]{32}", proof["comparison_set_id"])
        and isinstance(proof.get("comparison_set_fingerprint"), str)
        and re.fullmatch(r"[0-9a-f]{64}", proof["comparison_set_fingerprint"])
    )
