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
from typing import Any, Mapping

DRIVER_PLACEMENT_POLICY_ID = "driver_same_distance_v1"
# Deliberately a new policy id: evidence captured under the old
# ``summed_listening_position_v1`` instruction did not bind the microphone to
# the crossover's reference axis or promise that it would remain fixed across
# the normal/reverse pair.  It remains historical evidence, never automatic
# alignment evidence.
SUMMED_PLACEMENT_POLICY_ID = "summed_reference_axis_v1"
REFERENCE_AXIS_DRIVER_PLACEMENT_POLICY_ID = "driver_reference_axis_v1"
# A THIRD summed policy, and deliberately its own id rather than a reworded
# ``summed_reference_axis_v1``: the guided spatial cloud
# (docs/historical/linearization-campaign-2026-07.md fundamental 1) asks the household for the
# OPPOSITE whole-session promise. The stationary policy promises the mic does
# not move between captures; this one promises the mic starts on the reference
# axis, moves only when prompted, and holds still for the duration of each
# sweep. Consenting to one is not consenting to the other, so they cannot share
# an id — and the stationary id must stay reachable, because the 1-entry
# re-verify re-arm still makes exactly the stationary promise.
CLOUD_WALK_PLACEMENT_POLICY_ID = "summed_guided_cloud_v1"
COMPARISON_SET_SCHEMA_VERSION = 2
PLACEMENT_PROOF_SCHEMA_VERSION = 1
DRIVER_PLACEMENT_TARGET_CM = 3.0

# Capture protocol versions carrying the acknowledgement machinery a placement
# proof depends on. Protocol 1 has none, so it is excluded; 2 and 3 authenticate
# the SAME acknowledgement through the SAME
# validate_capture_acknowledgement/on_armed choreography.
#
# **2 stays even though the Pi no longer EMITS it.** This reads a version
# stamped into PERSISTED evidence by whatever page wrote the proof --
# `normalized_placement_proof` records the PAGE's `capture_protocol_version`,
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
# crossover_v2.sweep_spec imports THIS module (lazily, for placement copy), so
# importing it back at module scope would invert that dependency. Containment
# of its CAPTURE_PROTOCOL_VERSION is pinned by
# tests/test_active_speaker_commissioning_capture.py.
PLACEMENT_PROOF_ACKNOWLEDGEMENT_CAPABLE_PROTOCOLS = (2, 3)

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
    # Output-topology ids may contain ``:`` but never ``/``.  Using the
    # forbidden character at this namespace boundary keeps the fixed-axis
    # controller id disjoint from every legal near-field ``group:role`` id.
    return f"reference_axis/{target_id}", repeat_fingerprint


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


# --- Aiming the microphone ---------------------------------------------------
# The three fixed-axis instructions below used to end "Aim it according to its
# calibration file." — an instruction a phone cannot follow, because a phone mic
# has no calibration file (`jasper.web.correction_setup._relay_calibration_from_
# setup` returns None for exactly that case) and a phone is the mic most
# households bring. So the copy names the physical aim direction the way this
# module's own near-field instruction already does ("pointed straight at it"),
# and demotes the calibration file to the conditional it always was: a UMIK-2
# owner who loaded the 90° curve really is told to aim elsewhere.
#
# Device-agnostic rather than conditional-on-tier BY NECESSITY, not preference:
# calibration presence is not knowable where this copy is rendered. Both call
# sites — `crossover_v2.sweep_spec.build_crossover_sweep_spec` and
# `web.correction_setup`'s fixed-axis level target — build the string before the
# household has chosen a mic, and the one calibration-shaped argument in reach
# (`default_setup_calibration`) is an optional prefill HINT that callers omit
# even when a calibration exists. Plumbing real calibration state through two
# layers to vary one clause would buy a worse sentence than one honest one.
_AIM_CLAUSE = "pointed at the speaker unless its calibration file says otherwise"


def reference_axis_driver_placement_instruction(role: str) -> str:
    """Canonical stationary axis shared by each isolated-driver capture."""

    role = str(role or "driver").strip().lower()
    return (
        "Place the microphone capsule on the tweeter axis, exactly level with "
        "the centre of the tweeter or horn mouth, about 1 metre away when the "
        f"room permits, {_AIM_CLAUSE}. Keep the "
        f"microphone and speaker completely still while measuring the {role} "
        "and every other driver in this set."
    )


def summed_placement_instruction() -> str:
    """Canonical fixed-axis placement for combined-driver alignment evidence."""

    return (
        "Place the microphone capsule on the tweeter axis, exactly level with "
        "the centre of the tweeter or horn mouth, about 1 metre away when the "
        f"room permits, {_AIM_CLAUSE}. Then keep the "
        "microphone and speaker completely still for every normal- and "
        "reverse-polarity combined-driver capture in this measurement set."
    )


def cloud_walk_placement_instruction() -> str:
    """Placement copy for the guided spatial cloud (``CLOUD_WALK_...`` policy).

    Answers exactly ONE of the orientation screen's questions — *where do I
    stand?* — and stops (issue #1941 R1). Same starting point as
    :func:`summed_placement_instruction` (the mark, on the tweeter axis), but
    it must NOT repeat the stationary copy's whole-session stillness promise:
    this session prompts the household to move the mic between captures, so
    promising otherwise on the consent screen would be asking them to agree to
    something the flow immediately contradicts. Per-sweep stillness — the
    promise an individual capture really does depend on — is its own step, and
    is unchanged.

    **It takes no capture count on purpose.** It used to open with "Across
    about ``{captures}`` measurements…", one line under a derived tier line
    that had just said "…10 measurements, about 7 minutes" — the same number,
    twice, in the densest block on the screen. The tier line
    (``sweep_spec._guided_tier_step``) owns that number; this owns the mark. The
    other two facts it used to carry moved to where they earn their keep:
    *every position is measured from the mark, named with a distance* now
    motivates the tape measure in the what-to-bring step, and *how far the
    walk reaches* is the shape note
    (:func:`~jasper.active_speaker.crossover_v2_flow.cloud_walk_shape`).
    """
    return (
        "Start with the microphone capsule on the tweeter axis, exactly level "
        "with the centre of the tweeter or horn mouth, about 1 metre away when "
        f"the room permits, {_AIM_CLAUSE} — that spot is your mark."
    )


def cloud_walk_acknowledgement_label(captures: int) -> str:
    """The promise the operator makes before a guided-cloud session.

    Deliberately promises only what the cloud actually needs — the starting
    axis, per-sweep stillness, and following the prompts — instead of the
    stationary policy's "I will not move it", which this flow asks them to
    break by design.
    """
    return (
        "The microphone starts on the tweeter axis, level with the centre of "
        "the tweeter or horn mouth, and I will move it only when I am asked "
        f"to, holding it still while each of the {int(captures)} sweeps plays."
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
