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

from typing import Any, Mapping

from jasper.json_fields import as_mapping

from ._common import coerce_finite_float
from .profile import ActiveSpeakerConfigError, ActiveSpeakerPreset, required_driver_roles

TUNING_OWNERS = frozenset({"manual", "automatic"})
REASON_APPLIED_GRADE_MARK_ONLY = "applied_grade_mark_only"



def measured_level_match_applied(snapshot: Mapping[str, Any]) -> bool:
    """Does this profile carry an applied level match backed by measurement?

    ANY role sourced ``measured`` is enough, deliberately: an operator pinning
    one driver does not un-measure the speaker, and the whole profile is still
    the product of a measured level match.

    Extracted so the two consumers of this question cannot drift apart.
    :func:`_snapshot_owner` below decides Layer-A ownership with it, and
    ``baseline_profile._bank_applied_base_trim`` decides whether an applied
    profile still counts as measured evidence — those answers disagreeing is
    how a mixed candidate came to CLEAR a bank the contract considered
    automatic.
    """

    sources = {
        str(value)
        for value in as_mapping(snapshot.get("corrections_source")).values()
    }
    return (
        as_mapping(snapshot.get("level_match")).get("applied") is True
        and "measured" in sources
    )


def _snapshot_owner(profile: Mapping[str, Any], snapshot: Mapping[str, Any]) -> str:
    owner = str(snapshot.get("tuning_owner") or profile.get("tuning_owner") or "")
    if owner in TUNING_OWNERS:
        return owner
    return "automatic" if measured_level_match_applied(snapshot) else "manual"


def crossover_snapshot_state(
    profile: Mapping[str, Any] | None,
    *,
    expected_topology_id: str | None = None,
    expected_topology_fingerprint: str | None = None,
    expected_domain: str = "full",
    require_applied: bool = True,
) -> dict[str, Any]:
    """Validate immutable Layer-A ownership and return one stable verdict."""
    profile = as_mapping(profile)
    snapshot = as_mapping(profile.get("recomposition_snapshot"))
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
    elif (expected_topology_fingerprint
          and snapshot.get("topology_fingerprint") != expected_topology_fingerprint):
        reason = "active_applied_profile_snapshot_topology_stale"
        detail = "The applied crossover no longer matches the output topology."
    else:
        try:
            preset = ActiveSpeakerPreset.from_mapping(dict(snapshot.get("preset") or {}))
        except (ActiveSpeakerConfigError, TypeError, ValueError):
            reason = "active_applied_profile_snapshot_invalid"
            detail = "The applied crossover snapshot has invalid speaker filters."
        else:
            corrections = as_mapping(snapshot.get("corrections"))
            roles = required_driver_roles(preset.way_count)
            if set(corrections) != set(roles):
                reason = "active_applied_profile_snapshot_invalid"
                detail = "The applied crossover snapshot is missing driver corrections."
            else:
                for role in roles:
                    correction = as_mapping(corrections.get(role))
                    gain = coerce_finite_float(correction.get("gain_db"))
                    delay = coerce_finite_float(correction.get("delay_ms"))
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
    applied = as_mapping(applied_profile)
    source = as_mapping(applied.get("source"))
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
