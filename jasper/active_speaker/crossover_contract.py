# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pure ownership and readiness contract for an applied crossover graph.

This module is the domain-owned single source of truth shared by setup status,
the crossover wizard, and the apply transaction.  It deliberately accepts
plain mappings so those callers can expose the same decision without acquiring
one another's I/O responsibilities.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping

from .profile import ActiveSpeakerConfigError, ActiveSpeakerPreset, required_driver_roles

TUNING_OWNERS = frozenset({"manual", "automatic"})


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _finite_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _snapshot_owner(profile: Mapping[str, Any], snapshot: Mapping[str, Any]) -> str:
    owner = str(snapshot.get("tuning_owner") or profile.get("tuning_owner") or "")
    if owner in TUNING_OWNERS:
        return owner
    sources = {
        str(value)
        for value in _mapping(snapshot.get("corrections_source")).values()
    }
    level_match = _mapping(snapshot.get("level_match"))
    return (
        "automatic"
        if level_match.get("applied") is True and "measured" in sources
        else "manual"
    )


def crossover_snapshot_state(
    profile: Mapping[str, Any] | None,
    *,
    expected_topology_id: str | None = None,
    expected_topology_fingerprint: str | None = None,
    expected_domain: str = "full",
    require_applied: bool = True,
) -> dict[str, Any]:
    """Validate immutable Layer-A ownership and return one stable verdict."""
    profile = _mapping(profile)
    snapshot = _mapping(profile.get("recomposition_snapshot"))
    owner = _snapshot_owner(profile, snapshot) if snapshot else None
    reason: str | None = None
    detail: str

    if require_applied and profile.get("status") != "applied":
        reason = "active_crossover_profile_not_applied"
        detail = "Apply a crossover profile before continuing."
    elif not snapshot:
        reason = "active_applied_profile_snapshot_missing"
        detail = "The applied crossover predates immutable graph snapshots."
    elif snapshot.get("schema_version") != 1:
        reason = "active_applied_profile_snapshot_invalid"
        detail = "The applied crossover snapshot schema is not supported."
    elif snapshot.get("domain") != expected_domain:
        reason = "active_applied_profile_snapshot_domain_invalid"
        detail = f"The crossover snapshot is not a valid {expected_domain} graph."
    elif expected_topology_id and snapshot.get("topology_id") != expected_topology_id:
        reason = "active_applied_profile_snapshot_topology_stale"
        detail = "The applied crossover belongs to a different output topology."
    elif (
        expected_topology_fingerprint
        and snapshot.get("topology_fingerprint") != expected_topology_fingerprint
    ):
        reason = "active_applied_profile_snapshot_topology_stale"
        detail = "The applied crossover no longer matches the output topology."
    else:
        try:
            preset = ActiveSpeakerPreset.from_mapping(dict(snapshot.get("preset") or {}))
        except (ActiveSpeakerConfigError, TypeError, ValueError):
            reason = "active_applied_profile_snapshot_invalid"
            detail = "The applied crossover snapshot has invalid speaker filters."
        else:
            corrections = _mapping(snapshot.get("corrections"))
            roles = required_driver_roles(preset.way_count)
            if set(corrections) != set(roles):
                reason = "active_applied_profile_snapshot_invalid"
                detail = "The applied crossover snapshot is missing driver corrections."
            else:
                for role in roles:
                    correction = _mapping(corrections.get(role))
                    gain = _finite_float(correction.get("gain_db"))
                    delay = _finite_float(correction.get("delay_ms"))
                    if (
                        gain is None
                        or not -60.0 <= gain <= 0.0
                        or delay is None
                        or not 0.0 <= delay <= 20.0
                        or not isinstance(correction.get("inverted"), bool)
                    ):
                        reason = "active_applied_profile_snapshot_invalid"
                        detail = f"The applied crossover correction for {role} is unsafe."
                        break
                else:
                    playback_device = snapshot.get("playback_device")
                    if not isinstance(playback_device, str) or not playback_device:
                        reason = "active_applied_profile_snapshot_invalid"
                        detail = "The applied crossover snapshot has no playback device."
                    else:
                        detail = f"The applied {owner} crossover snapshot is valid."

    return {
        "valid": reason is None,
        "reason": reason,
        "detail": detail,
        "owner": owner,
        "snapshot_available": bool(snapshot),
    }


def legacy_manual_preservation_state(
    applied_profile: Mapping[str, Any] | None,
    *,
    current_source_fingerprint: str | None,
) -> dict[str, Any]:
    """Whether a legacy manual graph can be snapshotted without filter drift."""
    applied = _mapping(applied_profile)
    source = _mapping(applied.get("source"))
    applied_fingerprint = str(source.get("fingerprint") or "")
    current_fingerprint = str(current_source_fingerprint or "")
    legacy = bool(
        applied.get("status") == "applied"
        and not isinstance(applied.get("recomposition_snapshot"), Mapping)
    )
    exact_match = bool(
        legacy
        and applied_fingerprint
        and current_fingerprint
        and applied_fingerprint == current_fingerprint
    )
    reason = None if exact_match else (
        "manual_crossover_not_legacy_applied"
        if not legacy
        else "manual_crossover_source_changed"
    )
    detail = (
        "The currently applied manual crossover can be preserved exactly."
        if exact_match
        else (
            "The saved crossover inputs changed after this manual crossover was "
            "applied. Edit and apply the manual crossover again, or tune automatically."
        )
    )
    return {
        "ready": exact_match,
        "reason": reason,
        "detail": detail,
        "applied_source_fingerprint": applied_fingerprint or None,
        "current_source_fingerprint": current_fingerprint or None,
    }


def automatic_candidate_readiness(
    *,
    required_group_ids: Iterable[str],
    level_match: Mapping[str, Any] | None,
    measurement_summary: Mapping[str, Any] | None,
    active_comparison_set: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return whether current acoustic evidence can produce an automatic profile."""
    required = {str(group_id) for group_id in required_group_ids if str(group_id)}
    level_match = _mapping(level_match)
    summary = _mapping(measurement_summary)
    incomparable = level_match.get("incomparable_groups")
    incomparable = incomparable if isinstance(incomparable, list) else []
    measured_group_ids = level_match.get("measured_group_ids")
    measured_ids = {
        str(group_id)
        for group_id in (
            measured_group_ids
            if isinstance(measured_group_ids, (list, tuple, set))
            else []
        )
        if str(group_id)
    }
    measured_count = _nonnegative_int(level_match.get("groups_measured"))
    driver_groups_ready = (
        required.issubset(measured_ids)
        if measured_ids
        else measured_count >= len(required)
    )
    latest_summed = _mapping(summary.get("latest_summed_validations"))
    from .capture_geometry import (
        SUMMED_PLACEMENT_POLICY_ID,
        capture_proof_valid,
    )

    summed_ready: set[str] = set()
    for group_id, record in latest_summed.items():
        record = _mapping(record)
        acoustic = _mapping(record.get("acoustic"))
        if (
            record.get("validated") is True
            and acoustic.get("verdict") == "blend_ok"
            and record.get("mic_clipping") is not True
            and acoustic.get("mic_clipping") is not True
            and capture_proof_valid(
                record,
                active_comparison_set,
                policy_id=SUMMED_PLACEMENT_POLICY_ID,
                role="summed",
                speaker_group_id=str(group_id),
                target_fingerprint=str(record.get("group_fingerprint") or ""),
            )
        ):
            summed_ready.add(str(group_id))

    if not required:
        reason = "automatic_crossover_not_applicable"
        detail = "This topology has no active crossover groups to tune."
    elif incomparable:
        reason = "automatic_crossover_measurements_incomparable"
        detail = (
            "Repeat the driver sweeps in one guided run so microphone placement, "
            "level, and excitation can be compared."
        )
    elif level_match.get("applied") is not True or not driver_groups_ready:
        reason = "automatic_crossover_measurements_incomplete"
        detail = "Finish usable driver sweeps before applying automatic tuning."
    elif not required.issubset(summed_ready):
        reason = "automatic_crossover_summed_measurement_incomplete"
        detail = "Finish the combined-crossover sweep before applying automatic tuning."
    else:
        reason = None
        detail = "The automatic crossover candidate has complete usable acoustic evidence."

    return {
        "ready": reason is None,
        "reason": reason,
        "detail": detail,
        "required_group_ids": sorted(required),
        "measured_group_ids": sorted(measured_ids),
        "summed_group_ids": sorted(summed_ready),
        "measurement_comparable": not incomparable,
        # Compatibility alias for existing status consumers.
        "excitation_comparable": not incomparable,
    }
