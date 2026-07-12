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

from ._common import REGION_FC_MATCH_TOLERANCE_HZ
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


def summed_decision_evidence_state(
    record: Mapping[str, Any] | None,
    *,
    active_comparison_set: Mapping[str, Any] | None,
    expected_applied_profile: Mapping[str, Any] | None,
    speaker_group_id: str,
    lower_role: str,
    upper_role: str,
    crossover_fc_hz: float,
    expected_expect_null: bool,
    expected_profile_context_id: str | None,
) -> dict[str, Any]:
    """Admit one summed capture as automatic alignment decision evidence.

    Summed records remain useful history even when this returns ``valid=False``.
    This is the single decision boundary: it re-proves the audible playback,
    analyzer outcome, the exact applied-graph excitation ledger, current
    profile/comparison binding, fixed reference-axis placement, and exact
    crossover region before a null depth may authorize a polarity or delay
    status.

    ``validated=False`` is not automatically invalid: a cleanly captured
    ``polarity_or_delay_problem`` is precisely the negative evidence an
    alignment proposal must inspect.  The flag must instead be consistent with
    the acoustic outcome, and the record must contain no blocker issue.
    """

    def refused(reason: str) -> dict[str, Any]:
        return {"valid": False, "reason": reason}

    if not isinstance(record, Mapping):
        return refused("summed_evidence_missing")
    if (
        not isinstance(expected_profile_context_id, str)
        or not expected_profile_context_id
        or not isinstance(active_comparison_set, Mapping)
        or active_comparison_set.get("profile_context_id")
        != expected_profile_context_id
    ):
        return refused("summed_evidence_profile_context_stale")
    group_id = str(speaker_group_id or "")
    if record.get("speaker_group_id") != group_id:
        return refused("summed_evidence_group_mismatch")
    group_fingerprint = record.get("group_fingerprint")
    if not isinstance(group_fingerprint, str) or not group_fingerprint:
        return refused("summed_evidence_target_fingerprint_missing")

    issues = record.get("issues")
    if not isinstance(issues, list) or not all(
        isinstance(issue, Mapping)
        and issue.get("severity") in {"warning", "blocker"}
        for issue in issues
    ):
        return refused("summed_evidence_issues_invalid")
    if any(issue.get("severity") == "blocker" for issue in issues):
        return refused("summed_evidence_blocked")

    outcome = record.get("outcome")
    if outcome not in {"blend_ok", "polarity_or_delay_problem"}:
        return refused("summed_evidence_outcome_invalid")
    expected_validated = outcome == "blend_ok"
    if record.get("validated") is not expected_validated:
        return refused("summed_evidence_validation_status_invalid")
    if record.get("mic_clipping") is not False:
        return refused("summed_evidence_clipped")
    mic_meter = record.get("mic_meter")
    if (
        not isinstance(mic_meter, Mapping)
        or mic_meter.get("status") not in {"too_quiet", "low", "usable"}
    ):
        return refused("summed_evidence_mic_level_invalid")

    acoustic = record.get("acoustic")
    if not isinstance(acoustic, Mapping):
        return refused("summed_evidence_acoustic_missing")
    if acoustic.get("verdict") != outcome:
        return refused("summed_evidence_acoustic_outcome_mismatch")
    if acoustic.get("mic_clipping") is not False:
        return refused("summed_evidence_clipped")
    if acoustic.get("expect_null") is not expected_expect_null:
        return refused("summed_evidence_polarity_slot_mismatch")
    if not isinstance(acoustic.get("calibrated"), bool):
        return refused("summed_evidence_calibration_status_missing")
    null_depth = _finite_float(acoustic.get("null_depth_db"))
    if null_depth is None or null_depth < 0:
        return refused("summed_evidence_null_depth_invalid")
    acoustic_fc = _finite_float(acoustic.get("crossover_fc_hz"))
    if (
        acoustic_fc is None
        or abs(acoustic_fc - float(crossover_fc_hz))
        >= REGION_FC_MATCH_TOLERANCE_HZ
    ):
        return refused("summed_evidence_acoustic_fc_mismatch")

    summed_test = record.get("summed_test")
    if not isinstance(summed_test, Mapping):
        return refused("summed_evidence_playback_missing")
    record_test_id = str(record.get("summed_test_id") or "")
    test_ids = {
        str(summed_test.get("summed_test_id") or ""),
        str(summed_test.get("playback_id") or ""),
    }
    if (
        not record_test_id
        or record_test_id not in test_ids
        or summed_test.get("captured") is not True
        or summed_test.get("audio_emitted") is not True
        or summed_test.get("group_fingerprint") != group_fingerprint
    ):
        return refused("summed_evidence_playback_invalid")

    # The earlier summed-test artifact proves the intended outputs were audible,
    # but it is not the ESS playback that produced this acoustic record.  Bind
    # automatic evidence to the normalized ledger emitted by that exact sweep
    # and re-prove it against the immutable applied graph.  Without this check a
    # hand-built/legacy record can carry a persuasive null while saying nothing
    # about the gain, delay, or polarity that actually drove the loudspeakers.
    applied_profile = _mapping(expected_applied_profile)
    applied_source = _mapping(applied_profile.get("source"))
    applied_snapshot = _mapping(applied_profile.get("recomposition_snapshot"))
    expected_corrections = _mapping(applied_snapshot.get("corrections"))
    expected_topology_id = applied_snapshot.get("topology_id")
    expected_baseline_id = applied_profile.get("baseline_id")
    if (
        applied_profile.get("status") != "applied"
        or applied_source.get("fingerprint") != expected_profile_context_id
        or not isinstance(expected_topology_id, str)
        or not expected_topology_id
        or not isinstance(expected_baseline_id, str)
        or not expected_baseline_id
        or not expected_corrections
    ):
        return refused("summed_evidence_applied_graph_missing")

    excitation = record.get("excitation")
    if not isinstance(excitation, Mapping):
        return refused("summed_evidence_excitation_missing")
    sweep_peak = _finite_float(excitation.get("sweep_peak_dbfs"))
    played_corrections = excitation.get("corrections")
    if (
        excitation.get("schema_version") != 1
        or excitation.get("scope") != "sweep_plus_applied_full_layer_a_graph"
        or excitation.get("topology_id") != expected_topology_id
        or excitation.get("baseline_id") != expected_baseline_id
        or not isinstance(excitation.get("gain_source"), str)
        or not excitation.get("gain_source")
        or sweep_peak is None
        or not -120.0 <= sweep_peak <= 0.0
        or not isinstance(played_corrections, Mapping)
        or set(played_corrections) != set(expected_corrections)
    ):
        return refused("summed_evidence_excitation_invalid")
    for role, expected_raw in expected_corrections.items():
        played = _mapping(played_corrections.get(role))
        expected = _mapping(expected_raw)
        played_gain = _finite_float(played.get("gain_db"))
        played_delay = _finite_float(played.get("delay_ms"))
        played_effective = _finite_float(played.get("effective_peak_dbfs"))
        expected_gain = _finite_float(expected.get("gain_db"))
        expected_delay = _finite_float(expected.get("delay_ms"))
        if (
            played_gain is None
            or not -60.0 <= played_gain <= 0.0
            or played_delay is None
            or not 0.0 <= played_delay <= 20.0
            or played_effective is None
            or played_effective > 0.0
            or abs(played_effective - (sweep_peak + played_gain)) > 1e-6
            or expected_gain is None
            or expected_delay is None
            or abs(played_gain - expected_gain) > 1e-6
            or abs(played_delay - expected_delay) > 1e-6
            or not isinstance(played.get("inverted"), bool)
            or not isinstance(expected.get("inverted"), bool)
            or played.get("inverted") is not expected.get("inverted")
        ):
            return refused("summed_evidence_excitation_graph_mismatch")

    region = record.get("region")
    if not isinstance(region, Mapping):
        return refused("summed_evidence_region_missing")
    region_fc = _finite_float(region.get("fc_hz"))
    if (
        region.get("lower_role") != lower_role
        or region.get("upper_role") != upper_role
        or region_fc is None
        or abs(region_fc - float(crossover_fc_hz))
        >= REGION_FC_MATCH_TOLERANCE_HZ
    ):
        return refused("summed_evidence_region_mismatch")

    from .capture_geometry import (
        SUMMED_PLACEMENT_POLICY_ID,
        capture_proof_valid,
    )

    if not capture_proof_valid(
        record,
        active_comparison_set,
        policy_id=SUMMED_PLACEMENT_POLICY_ID,
        role="summed",
        speaker_group_id=group_id,
        target_fingerprint=group_fingerprint,
    ):
        return refused("summed_evidence_comparison_or_placement_invalid")
    return {"valid": True, "reason": None}


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
    else:
        reason = None
        detail = (
            "The automatic driver-level candidate has complete usable acoustic "
            "evidence. Crossover frequency and slope remain operator-owned."
        )

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
