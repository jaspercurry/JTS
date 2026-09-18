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
from typing import Any, Mapping

from ._common import finite_float as _finite_float
from .profile import ActiveSpeakerConfigError, ActiveSpeakerPreset, required_driver_roles

TUNING_OWNERS = frozenset({"manual", "automatic"})
REASON_APPLIED_GRADE_MARK_ONLY = "applied_grade_mark_only"
DRIVER_EXCITATION_MATCH_TOLERANCE_DB = 0.05
_DRIVER_EXCITATION_SCOPES = frozenset({
    "sweep_plus_role_varying_commission_gain",
    "sweep_plus_role_gain_and_driver_level_lock",
})


def verified_driver_excitation(value: Any) -> dict[str, Any] | None:
    """Audit arithmetic and attenuation-only bounds for one driver ledger."""

    if not isinstance(value, Mapping):
        return None
    scope = value.get("scope")
    if (
        type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
        or not isinstance(scope, str)
        or scope not in _DRIVER_EXCITATION_SCOPES
    ):
        return None

    def number(name: str) -> float | None:
        raw = value.get(name)
        if (
            isinstance(raw, bool)
            or not isinstance(raw, (int, float))
            or not math.isfinite(float(raw))
        ):
            return None
        return float(raw)

    sweep_peak = number("sweep_peak_dbfs")
    commissioning_gain = number("commissioning_gain_db")
    effective = number("effective_peak_dbfs")
    if scope == "sweep_plus_role_gain_and_driver_level_lock":
        locked_main_volume = number("locked_main_volume_db")
    else:
        if "locked_main_volume_db" in value:
            return None
        locked_main_volume = 0.0
    if any(
        item is None
        for item in (sweep_peak, commissioning_gain, effective, locked_main_volume)
    ):
        return None
    assert sweep_peak is not None
    assert commissioning_gain is not None
    assert effective is not None
    assert locked_main_volume is not None
    canonical_effective = sweep_peak + commissioning_gain + locked_main_volume
    if (
        abs(canonical_effective - effective)
        > DRIVER_EXCITATION_MATCH_TOLERANCE_DB
    ):
        return None
    if (
        sweep_peak > 0.0
        or commissioning_gain > 0.0
        or locked_main_volume > 0.0
        or canonical_effective > 0.0
        or effective > 0.0
    ):
        return None
    for name in ("gain_source", "topology_id", "role"):
        if name in value and (
            not isinstance(value[name], str) or not value[name].strip()
        ):
            return None
    result = {
        "schema_version": 1,
        "scope": scope,
        "sweep_peak_dbfs": sweep_peak,
        "commissioning_gain_db": commissioning_gain,
        "effective_peak_dbfs": canonical_effective,
        **{
            name: value[name]
            for name in ("gain_source", "topology_id", "role")
            if name in value
        },
    }
    if scope == "sweep_plus_role_gain_and_driver_level_lock":
        result["locked_main_volume_db"] = locked_main_volume
    return result


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def preset_matches_applied_profile(
    preset: ActiveSpeakerPreset,
    applied_profile: Mapping[str, Any] | None,
    *,
    candidate_corrections: Mapping[str, Any] | None = None,
) -> bool:
    """Return whether ``preset`` is the exact graph context that was measured.

    The comparison-set ``profile_context_id`` binds captures to the protected
    applied profile, but Fc/role identity alone cannot detect a mutable preview
    that changed family, order, trim, polarity, or delay at the same Fc.  The
    immutable recomposition snapshot is the canonical applied preset; fail
    closed when it is absent or cannot be parsed.
    """

    profile = _mapping(applied_profile)
    snapshot = _mapping(profile.get("recomposition_snapshot"))
    raw_preset = snapshot.get("preset")
    if not isinstance(raw_preset, dict):
        return False
    try:
        applied_preset = ActiveSpeakerPreset.from_mapping(raw_preset)
        applied_preset.validate()
        preset.validate()
    except (ActiveSpeakerConfigError, TypeError, ValueError):
        return False
    if preset.to_dict() != applied_preset.to_dict():
        return False
    if candidate_corrections is None:
        return True
    applied_corrections = snapshot.get("corrections")
    if not isinstance(applied_corrections, Mapping):
        return False
    roles = required_driver_roles(preset.way_count)
    for role in roles:
        candidate = _mapping(candidate_corrections.get(role))
        applied = _mapping(applied_corrections.get(role))
        candidate_gain = _finite_float(candidate.get("gain_db"))
        candidate_delay = _finite_float(candidate.get("delay_ms"))
        applied_gain = _finite_float(applied.get("gain_db"))
        applied_delay = _finite_float(applied.get("delay_ms"))
        if (
            candidate_gain is None
            or candidate_delay is None
            or applied_gain is None
            or applied_delay is None
            or abs(candidate_gain - applied_gain) > 1e-6
            or abs(candidate_delay - applied_delay) > 1e-6
            or not isinstance(candidate.get("inverted"), bool)
            or not isinstance(applied.get("inverted"), bool)
            or candidate.get("inverted") is not applied.get("inverted")
        ):
            return False
    return True


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
        for value in _mapping(snapshot.get("corrections_source")).values()
    }
    return (
        _mapping(snapshot.get("level_match")).get("applied") is True
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
