# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Durable active-speaker driver-check and measurement evidence.

This module records the evidence produced by the guided active-crossover flow:
one measured result per driver and one summed crossover validation per active
speaker group. It does not play tones, capture audio, load CamillaDSP, or infer
acoustic truth from thin evidence. It stores what the UI and operator observed
so the baseline compiler can decide whether it has enough evidence to proceed.

**Paired summed evidence (Slice 2).** A summed crossover region can be
measured twice at the same fixed position — once in-phase (a correct
crossover sums flat) and once with one driver deliberately reversed (a
correct crossover then cancels deeply). Both readings are real, distinct
evidence, but `record_summed_validation` -> `_summarise` used to keep only
ONE "latest" record per group regardless of polarity, so a reverse-polarity
capture recorded after an in-phase one silently overwrote it (and vice
versa) in every downstream summary field. `latest_summed_by_group` /
`latest_summed_validations` are now defined as the latest IN-PHASE record
per group specifically (see `_latest_current_summed_records`); paired
evidence — both polarities, keyed by crossover region — lives alongside it
in `latest_summed_pairs_by_group`.
"""

from __future__ import annotations

import json
import hashlib
import logging
import math
import os
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from jasper.atomic_io import atomic_write_text
from jasper.log_event import log_event
from jasper.output_topology import OutputTopology

from ._common import issue as _issue, region_key as _region_key
from .calibration_level import classify_mic_meter
from .profile import ADJACENT_PAIRS_BY_WAY
from .safe_playback import playback_target_signature

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
MEASUREMENT_STATE_KIND = "jts_active_speaker_measurements"
DEFAULT_STATE_PATH = Path("/var/lib/jasper/active_speaker_measurements.json")
STATE_PATH_ENV = "JASPER_ACTIVE_SPEAKER_MEASUREMENTS_STATE"

DRIVER_OUTCOMES = {
    "heard_correct_driver",
    "heard_wrong_driver",
    "silent",
    "too_loud",
}
SUMMED_OUTCOMES = {
    "blend_ok",
    "needs_adjustment",
    "polarity_or_delay_problem",
    "too_loud",
}
MAX_DRIVER_RECORDS = 48
MAX_SUMMED_RECORDS = 24
MAX_SUMMED_TEST_RECORDS = 24


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def measurement_state_path(path: str | Path | None = None) -> Path:
    return Path(path or os.environ.get(STATE_PATH_ENV) or DEFAULT_STATE_PATH)


def _finite_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _truthy_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


def _text(value: Any, *, max_chars: int = 240) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    out = " ".join(value.split())
    if not out:
        return None
    return out[:max_chars]


def _fingerprint(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _target_id(group_id: str, role: str) -> str:
    return f"{group_id}:{role}"


# A 2-way group's single crossover region is always woofer<->tweeter — the
# fixed, installation-independent vocabulary in
# jasper.active_speaker.profile.ADJACENT_PAIRS_BY_WAY[2] (a 2-way ALWAYS has
# exactly these two roles and exactly one region joining them). A summed
# record saved before region identity existed — or by a caller that never
# stamped one — resolves unambiguously into this one pair on a 2-way; see
# `_latest_current_summed_records`. A 3-way has two regions and no way to
# guess which one an unstamped record belongs to, so it is left out of
# pairing entirely in that case.
_TWO_WAY_REGION_KEY = _region_key(*ADJACENT_PAIRS_BY_WAY[2][0])


def _record_region_key(record: Mapping[str, Any]) -> str | None:
    """The paired-evidence key ``record`` belongs under, from its own stamp.

    Reads the ``region`` block ``commissioning_capture.record_summed_acoustic_
    capture`` stamps at record time (``{"lower_role", "upper_role", "fc_hz"}``,
    validated in `record_summed_validation`). ``None`` when the record has no
    resolvable region of its own — the caller decides whether a 2-way legacy
    fallback applies (see `_TWO_WAY_REGION_KEY`).
    """
    region = record.get("region")
    if not isinstance(region, Mapping):
        return None
    lower_role = region.get("lower_role")
    upper_role = region.get("upper_role")
    if (
        isinstance(lower_role, str) and lower_role
        and isinstance(upper_role, str) and upper_role
    ):
        return _region_key(lower_role, upper_role)
    return None


def _record_summed_kind(record: Mapping[str, Any]) -> str | None:
    """``"in_phase"`` / ``"reverse"`` / ``None`` (no acoustic verdict at all).

    A record with no ``acoustic`` block (the pure operator-listening-check
    path — no mic-backed verdict) has no polarity kind: it still counts
    toward ``latest_summed_by_group`` candidacy (a validated blend with no
    null evidence, same as before this pairing existed) but can never
    contribute to a region's in-phase/reverse pair.
    """
    acoustic = record.get("acoustic")
    if not isinstance(acoustic, Mapping):
        return None
    return "reverse" if _truthy_flag(acoustic.get("expect_null")) else "in_phase"


def _record_comparison_scope(record: Mapping[str, Any]) -> tuple[str, str | None]:
    """Authoritative commissioning-run scope carried by a capture record.

    Modern relay captures bind their server-normalized placement proof to the
    active comparison set.  That id is decision evidence (unlike the optional
    forensic bundle reference), so paired in-phase/reverse captures may use it
    to prove they came from the same fixed-position commissioning run.  Legacy
    records have no proof and return the legacy scope; they retain the
    pre-existing newest-per-polarity fallback within that legacy bucket. A
    present-but-malformed proof is distinct from legacy evidence and must not
    authorize any pair.
    """

    if "placement_proof" not in record or record.get("placement_proof") is None:
        return "legacy", None
    proof = record.get("placement_proof")
    if not isinstance(proof, Mapping):
        return "invalid", None
    value = proof.get("comparison_set_id")
    if not isinstance(value, str) or len(value) != 32:
        return "invalid", None
    if not all(ch in "0123456789abcdef" for ch in value):
        return "invalid", None
    return "comparison_set", value


def _valid_region(value: Any) -> dict[str, Any] | None:
    """Validate a caller-supplied ``region`` block before persisting it.

    ``value`` is ``commissioning_capture.record_summed_acoustic_capture``'s
    ``{"lower_role", "upper_role", "fc_hz"}`` stamp — two non-empty role
    strings and a finite, positive ``fc_hz``. Anything else (missing,
    malformed, unresolvable-fc ``None``) persists as ``None`` rather than a
    half-formed region a pair reader could misfile.
    """
    if not isinstance(value, Mapping):
        return None
    lower_role = value.get("lower_role")
    upper_role = value.get("upper_role")
    fc_hz = _finite_float(value.get("fc_hz"))
    if (
        isinstance(lower_role, str) and lower_role
        and isinstance(upper_role, str) and upper_role
        and fc_hz is not None and fc_hz > 0
    ):
        return {"lower_role": lower_role, "upper_role": upper_role, "fc_hz": fc_hz}
    return None


def _active_groups(topology: OutputTopology) -> list[Any]:
    return [
        group for group in topology.speaker_groups
        if group.mode in {"active_2_way", "active_3_way"}
    ]


def _hardware_payload(topology: OutputTopology) -> Mapping[str, Any]:
    return topology.hardware.to_dict()


def _target_fingerprint(
    topology: OutputTopology,
    target: Mapping[str, Any],
) -> str:
    """Fingerprint the physical output target that measurement evidence proves."""

    return _fingerprint({
        "topology_id": topology.topology_id,
        "hardware": _hardware_payload(topology),
        "speaker_group_id": target.get("speaker_group_id"),
        "speaker_group_kind": target.get("speaker_group_kind"),
        "speaker_group_mode": target.get("speaker_group_mode"),
        "role": target.get("role"),
        "output_index": target.get("output_index"),
        "identity_verified": bool(target.get("identity_verified")),
    })


def active_driver_targets(topology: OutputTopology) -> list[dict[str, Any]]:
    """Return the driver targets that need measurement evidence."""

    targets: list[dict[str, Any]] = []
    for group in _active_groups(topology):
        for channel in group.channels:
            target = {
                "target_id": _target_id(group.id, channel.role),
                "speaker_group_id": group.id,
                "speaker_group_label": group.label,
                "speaker_group_kind": group.kind,
                "speaker_group_mode": group.mode,
                "role": channel.role,
                "output_index": channel.physical_output_index,
                "output_label": (
                    channel.human_output_label
                    or (
                        f"DAC output {channel.physical_output_index + 1}"
                        if channel.physical_output_index is not None
                        else None
                    )
                ),
                "identity_verified": bool(channel.identity_verified),
            }
            target["target_fingerprint"] = _target_fingerprint(topology, target)
            targets.append(target)
    return targets


def _summed_fingerprint(
    topology: OutputTopology,
    group: Any,
    driver_targets: list[dict[str, Any]],
) -> str:
    return _fingerprint({
        "topology_id": topology.topology_id,
        "hardware": _hardware_payload(topology),
        "speaker_group_id": group.id,
        "speaker_group_kind": group.kind,
        "speaker_group_mode": group.mode,
        "driver_target_fingerprints": [
            target["target_fingerprint"]
            for target in driver_targets
            if target["speaker_group_id"] == group.id
        ],
    })


def active_summed_targets(topology: OutputTopology) -> list[dict[str, Any]]:
    """Return active speaker groups that need a summed crossover check."""

    driver_targets = active_driver_targets(topology)
    return [
        {
            "speaker_group_id": group.id,
            "speaker_group_label": group.label,
            "mode": group.mode,
            "roles": [channel.role for channel in group.channels],
            "group_fingerprint": _summed_fingerprint(
                topology,
                group,
                driver_targets,
            ),
        }
        for group in _active_groups(topology)
    ]


def _target_lookup(topology: OutputTopology) -> dict[str, dict[str, Any]]:
    return {target["target_id"]: target for target in active_driver_targets(topology)}


def _group_ids(topology: OutputTopology) -> set[str]:
    return {group.id for group in _active_groups(topology)}


def _summed_lookup(topology: OutputTopology) -> dict[str, dict[str, Any]]:
    return {
        target["speaker_group_id"]: target
        for target in active_summed_targets(topology)
    }


def _base_state(path: Path) -> dict[str, Any]:
    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": MEASUREMENT_STATE_KIND,
        "status": "not_started",
        "updated_at": None,
        "state_path": str(path),
        "driver_measurements": [],
        "summed_tests": [],
        "summed_validations": [],
        "latest_by_target": {},
        "latest_summed_tests": {},
        "latest_summed_by_group": {},
        "latest_summed_pairs_by_group": {},
        "active_comparison_set": None,
        "summary": {},
        "issues": [],
    }


def _normalise_records(raw: Any, *, limit: int) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    records = [item for item in raw if isinstance(item, dict)]
    return records[-limit:]


def _normalise_state(raw: Any, path: Path) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return _base_state(path)
    state = _base_state(path)
    state.update({
        key: raw.get(key)
        for key in state
        if key in raw
    })
    state["artifact_schema_version"] = SCHEMA_VERSION
    state["kind"] = MEASUREMENT_STATE_KIND
    state["state_path"] = str(path)
    state["driver_measurements"] = _normalise_records(
        raw.get("driver_measurements"),
        limit=MAX_DRIVER_RECORDS,
    )
    state["summed_tests"] = _normalise_records(
        raw.get("summed_tests"),
        limit=MAX_SUMMED_TEST_RECORDS,
    )
    state["summed_validations"] = _normalise_records(
        raw.get("summed_validations"),
        limit=MAX_SUMMED_RECORDS,
    )
    return state


def clear_active_comparison_set(
    topology: OutputTopology,
    *,
    state_path: str | Path | None = None,
) -> dict[str, Any]:
    """Invalidate prior automatic evidence before a new level run starts."""

    path = measurement_state_path(state_path)
    state = load_measurement_state(topology, state_path=path)
    persisted = _normalise_state(state, path)
    persisted["active_comparison_set"] = None
    persisted["updated_at"] = _utc_now()
    out = _with_summary(topology, persisted)
    _write_state(path, out)
    return out


def start_active_comparison_set(
    topology: OutputTopology,
    *,
    profile_context_id: str,
    setup_sha256: str,
    device_sha256: str,
    calibration_id: str,
    driver_level_locks: Mapping[str, Mapping[str, Any]],
    bundle_session_id: str | None = None,
    state_path: str | Path | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Persist the immutable mic and complete per-driver level context.

    ``bundle_session_id``, when supplied, joins this comparison set to a
    durable commissioning bundle (``jasper.active_speaker.bundles``) opened
    for the same run. It rides an extra key outside
    ``capture_geometry._COMPARISON_SET_CORE_KEYS``, so it does not change
    ``comparison_set_fingerprint`` or affect ``comparison_set_valid`` — the
    bundle is forensic evidence, never an input to any decision this state
    makes.
    """

    from .capture_geometry import (
        COMPARISON_SET_SCHEMA_VERSION,
        comparison_set_fingerprint,
        comparison_set_valid,
    )

    path = measurement_state_path(state_path)
    state = load_measurement_state(topology, state_path=path)
    created_at = now or _utc_now()
    expected_target_ids = {
        target["target_id"] for target in active_driver_targets(topology)
    }
    normalized_locks = {
        str(target_id): dict(lock)
        for target_id, lock in driver_level_locks.items()
    }
    if set(normalized_locks) != expected_target_ids:
        raise ValueError("driver level locks are incomplete for the active topology")
    core = {
        "schema_version": COMPARISON_SET_SCHEMA_VERSION,
        "comparison_set_id": uuid.uuid4().hex,
        "created_at": created_at,
        "topology_id": topology.topology_id,
        "profile_context_id": str(profile_context_id),
        "setup_sha256": str(setup_sha256),
        "device_sha256": str(device_sha256),
        "calibration_id": str(calibration_id or ""),
        "driver_level_locks": normalized_locks,
    }
    comparison_set = {**core, "fingerprint": comparison_set_fingerprint(core)}
    if bundle_session_id:
        comparison_set["bundle_session_id"] = str(bundle_session_id)
    if not comparison_set_valid(comparison_set):
        raise ValueError("driver level locks are malformed")
    persisted = _normalise_state(state, path)
    persisted["active_comparison_set"] = comparison_set
    persisted["updated_at"] = created_at
    out = _with_summary(topology, persisted)
    _write_state(path, out)
    event_fields: dict[str, Any] = {}
    # The optional session id is intentionally added after the comparison
    # fingerprint is built: it is a forensic join key, not comparison-critical
    # acoustic context.
    bundle_session_id = comparison_set.get("bundle_session_id")
    if bundle_session_id:
        event_fields["session"] = str(bundle_session_id)
    event_fields["group"] = ",".join(sorted(_group_ids(topology)))
    event_fields["calibration_id"] = comparison_set.get("calibration_id")
    event_fields["comparison_set_fingerprint"] = comparison_set.get("fingerprint")
    log_event(logger, "correction.crossover_session_started", **event_fields)
    return comparison_set


def _latest_by_key(
    records: list[dict[str, Any]],
    key: str,
) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for record in records:
        value = record.get(key)
        if isinstance(value, str) and value:
            latest[value] = record
    return latest


def _latest_current_driver_records(
    records: list[dict[str, Any]],
    targets: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], int]:
    target_by_id = {target["target_id"]: target for target in targets}
    latest: dict[str, dict[str, Any]] = {}
    stale_count = 0
    for record in reversed(records):
        target_id = record.get("target_id")
        if not isinstance(target_id, str) or target_id not in target_by_id:
            continue
        target = target_by_id[target_id]
        if record.get("target_fingerprint") == target.get("target_fingerprint"):
            latest.setdefault(target_id, record)
        else:
            stale_count += 1
    return latest, stale_count


def _latest_current_summed_records(
    records: list[dict[str, Any]],
    targets: list[dict[str, Any]],
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, dict[str, dict[str, dict[str, Any] | None]]],
    int,
]:
    """Latest current summed evidence, both flat (in-phase) and paired.

    Returns ``(latest_in_phase_by_group, latest_pairs_by_group, stale_count)``:

    * ``latest_in_phase_by_group`` is what every existing consumer reads as
      ``latest_summed_by_group`` / ``latest_summed_validations`` — the setup
      readiness blend gate (``setup_status._usable_summed_acoustic``), the
      automatic-candidate readiness check (``crossover_contract.
      automatic_candidate_readiness``), the automatic tuning tier's delay/
      polarity refinement (``baseline_profile._derive_corrections``), and the
      ``validated_groups`` completion check in ``_summarise`` below. All of
      those intend "does the crossover blend cleanly in phase?" — a reverse-
      polarity capture (``acoustic.expect_null`` true) answers a DIFFERENT
      question ("does it null when deliberately inverted?") and must not
      silently replace the in-phase answer just because it was captured more
      recently. Before this pairing existed, both kinds shared one
      latest-wins slot, so a reverse capture recorded after an in-phase one
      (or vice versa) silently overwrote it everywhere at once — this is the
      fix. A record with no ``acoustic`` block at all (pure operator
      listening check) still counts as in-phase-eligible, unchanged from
      prior behavior.
    * ``latest_pairs_by_group`` is ``{group_id: {region_key: {"in_phase":
      rec|None, "reverse": rec|None}}}``, newest-per-kind within the newest
      comparison set for that region, built only from records that carry a
      real acoustic verdict (a kind) AND a resolvable region — the record's
      own stamped ``region``, or, on a 2-way only, the legacy fallback in
      ``_TWO_WAY_REGION_KEY``. This prevents a new in-phase capture from being
      paired with an old reverse capture taken at a different placement/run.
      Legacy records without comparison-set proof pair only with other legacy
      records in this historical summary. They are never decision-authorizing:
      ``crossover_contract.summed_decision_evidence_state`` re-proves the full
      current comparison/profile, playback, placement, and region contract
      before the proposal reads a null. A 3-way's region-less legacy record has
      no unambiguous home and is left out of pairing (though it can still be
      in-phase-eligible above).
    """
    target_by_group = {target["speaker_group_id"]: target for target in targets}
    latest: dict[str, dict[str, Any]] = {}
    pairs: dict[str, dict[str, dict[str, dict[str, Any] | None]]] = {}
    pair_comparison_sets: dict[str, dict[str, tuple[str, str | int | None]]] = {}
    stale_count = 0
    for record_index, record in enumerate(reversed(records)):
        group_id = record.get("speaker_group_id")
        if not isinstance(group_id, str) or group_id not in target_by_group:
            continue
        target = target_by_group[group_id]
        if record.get("group_fingerprint") != target.get("group_fingerprint"):
            stale_count += 1
            continue
        kind = _record_summed_kind(record)
        if kind != "reverse":
            latest.setdefault(group_id, record)
        region_key = _record_region_key(record)
        if region_key is None and target.get("mode") == "active_2_way":
            region_key = _TWO_WAY_REGION_KEY
        if kind is not None and region_key is not None:
            scope_kind, comparison_set_id = _record_comparison_scope(record)
            comparison_scope: tuple[str, str | int | None] = (
                scope_kind,
                record_index if scope_kind == "invalid" else comparison_set_id,
            )
            group_pair_sets = pair_comparison_sets.setdefault(group_id, {})
            if region_key not in group_pair_sets:
                # Records are walked newest-first. The first record for this
                # region anchors the pair to its commissioning run; an older
                # run may not fill the missing polarity slot.
                group_pair_sets[region_key] = comparison_scope
            elif group_pair_sets[region_key] != comparison_scope:
                continue
            region_pairs = pairs.setdefault(group_id, {})
            slot = region_pairs.setdefault(
                region_key, {"in_phase": None, "reverse": None}
            )
            if scope_kind == "invalid":
                # Keep an authoritative empty region so 2-way consumers do
                # not mistake absence for legacy state and fall back to the
                # flat latest-in-phase compatibility slot.
                continue
            if slot[kind] is None:
                slot[kind] = record
    return latest, pairs, stale_count


def _latest_current_summed_tests(
    records: list[dict[str, Any]],
    targets: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], int]:
    target_by_group = {target["speaker_group_id"]: target for target in targets}
    latest: dict[str, dict[str, Any]] = {}
    stale_count = 0
    for record in reversed(records):
        group_id = record.get("speaker_group_id")
        if not isinstance(group_id, str) or group_id not in target_by_group:
            continue
        target = target_by_group[group_id]
        if record.get("group_fingerprint") == target.get("group_fingerprint"):
            latest.setdefault(group_id, record)
        else:
            stale_count += 1
    return latest, stale_count


def _record_playback_id(record: Mapping[str, Any]) -> str:
    value = record.get("summed_test_id") or record.get("playback_id")
    return str(value or "")


def _expected_summed_output_indices(
    topology: OutputTopology,
    speaker_group_id: str,
) -> list[int]:
    for group in _active_groups(topology):
        if group.id != speaker_group_id:
            continue
        indices: list[int] = []
        for channel in group.channels:
            if channel.physical_output_index is not None:
                indices.append(int(channel.physical_output_index))
        return sorted(set(indices))
    return []


def _output_indices_from_playback(playback: Mapping[str, Any]) -> list[int]:
    artifact = playback.get("artifact") if isinstance(playback.get("artifact"), Mapping) else {}
    raw_indices = artifact.get("target_output_indices")
    indices: list[int] = []
    if isinstance(raw_indices, list):
        candidates = raw_indices
    else:
        candidates = [artifact.get("target_output_index")]
    for value in candidates:
        try:
            output_index = int(value)
        except (TypeError, ValueError):
            continue
        if output_index >= 0:
            indices.append(output_index)
    return sorted(set(indices))


def _mic_meter_from(
    raw: Mapping[str, Any],
    calibration_level: Mapping[str, Any] | None,
) -> tuple[float | None, bool, dict[str, Any]]:
    observed = _finite_float(raw.get("observed_mic_dbfs"))
    clipping = bool(raw.get("mic_clipping"))
    if observed is None and calibration_level:
        meter = calibration_level.get("mic_meter")
        if isinstance(meter, Mapping):
            observed = _finite_float(meter.get("observed_dbfs"))
            clipping = clipping or meter.get("status") == "clipping"
    meter = classify_mic_meter(observed_dbfs=observed, clipping=clipping)
    return observed, clipping, meter


def _target_signature(target: Mapping[str, Any]) -> dict[str, Any] | None:
    return playback_target_signature({
        "speaker_group_id": target.get("speaker_group_id"),
        "role": target.get("role"),
        "driver_role": target.get("role"),
        "output_index": target.get("output_index"),
    })


def _safe_floor_result(
    safe_session: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    if not isinstance(safe_session, Mapping):
        return None
    quiet = safe_session.get("quiet_start")
    if not isinstance(quiet, Mapping):
        return None
    result = quiet.get("last_operator_result")
    return result if isinstance(result, Mapping) else None


def _floor_confirmation_issues(
    raw: Mapping[str, Any],
    target: Mapping[str, Any],
    safe_session: Mapping[str, Any] | None,
    durable_floor_confirmation: Mapping[str, Any] | None = None,
) -> list[dict[str, str]]:
    playback_id = _text(raw.get("playback_id"), max_chars=120)
    result = (
        durable_floor_confirmation
        if isinstance(durable_floor_confirmation, Mapping)
        else _safe_floor_result(safe_session)
    )
    expected_target = _target_signature(target)
    observed_target = playback_target_signature(
        result.get("target") if isinstance(result, Mapping) else None
    )
    issues: list[dict[str, str]] = []
    if not playback_id:
        issues.append(_issue(
            "blocker",
            "driver_measurement_playback_missing",
            "record a floor-level driver test before this counts as measured",
        ))
    if (
        durable_floor_confirmation is None
        and (
            not isinstance(safe_session, Mapping)
            or safe_session.get("status") != "armed"
        )
    ):
        issues.append(_issue(
            "blocker",
            "driver_measurement_safe_session_missing",
            "driver measurement requires an armed safe test session",
        ))
    if not result or result.get("accepted") is not True:
        issues.append(_issue(
            "blocker",
            "driver_measurement_floor_confirmation_missing",
            "confirm the correct driver at the quietest level before measuring it",
        ))
    elif str(result.get("playback_id") or "") != playback_id:
        issues.append(_issue(
            "blocker",
            "driver_measurement_playback_mismatch",
            "driver measurement must match the latest accepted floor test",
        ))
    if expected_target and observed_target != expected_target:
        issues.append(_issue(
            "blocker",
            "driver_measurement_target_mismatch",
            "driver measurement must match the output target that was just tested",
        ))
    return issues


def current_driver_floor_evidence(
    topology: OutputTopology,
    measurements: Mapping[str, Any],
    *,
    speaker_group_id: str,
    role: str,
) -> dict[str, Any]:
    """Validate durable identity/floor evidence from a current-state summary.

    The summary normally excludes stale records, but this authorization boundary
    independently resolves the current topology target and compares every
    identity field before trusting the embedded confirmation.
    """
    group_id = str(speaker_group_id or "").strip()
    role_id = str(role or "").strip().lower()
    target_id = _target_id(group_id, role_id)
    target = _target_lookup(topology).get(target_id)
    summary = measurements.get("summary")
    latest = summary.get("latest_driver_measurements") if isinstance(summary, Mapping) else None
    record = latest.get(target_id) if isinstance(latest, Mapping) else None
    source = "durable_current_driver_measurement"

    def refused(reason: str, detail: str) -> dict[str, Any]:
        return {
            "valid": False,
            "source": source,
            "reason": reason,
            "detail": detail,
            "record": record if isinstance(record, Mapping) else None,
        }

    if target is None or not isinstance(record, Mapping):
        return refused(
            "driver_floor_confirmation_required",
            "confirm this driver by ear before recording mic evidence",
        )
    playback_id = _text(record.get("playback_id"), max_chars=120)
    record_issues = record.get("issues")
    issues_well_formed = isinstance(record_issues, list) and all(
        isinstance(issue, Mapping)
        and issue.get("severity") in {"warning", "blocker"}
        for issue in record_issues
    )
    issues_blocker_free = issues_well_formed and not any(
        issue.get("severity") == "blocker" for issue in record_issues
    )
    if (
        record.get("captured") is not True
        or record.get("outcome") != "heard_correct_driver"
        or record.get("target_id") != target_id
        or record.get("target_fingerprint") != target.get("target_fingerprint")
        or record.get("speaker_group_id") != target.get("speaker_group_id")
        or record.get("role") != target.get("role")
        or record.get("output_index") != target.get("output_index")
        or not playback_id
        or not issues_blocker_free
    ):
        return refused(
            "driver_floor_confirmation_invalid",
            "the saved driver confirmation is incomplete; confirm the driver again",
        )
    confirmation = record.get("floor_confirmation")
    confirmation_issues = _floor_confirmation_issues(
        record,
        target,
        None,
        confirmation if isinstance(confirmation, Mapping) else None,
    )
    if confirmation_issues:
        return refused(
            "driver_floor_confirmation_invalid",
            "the saved driver confirmation is malformed; confirm the driver again",
        )
    return {
        "valid": True,
        "source": source,
        "reason": None,
        "detail": "current durable driver identity and floor evidence is accepted",
        "playback_id": playback_id,
        "confirmation": dict(confirmation),
        "record": dict(record),
    }


def _summarise(topology: OutputTopology, state: dict[str, Any]) -> dict[str, Any]:
    driver_targets = active_driver_targets(topology)
    summed_targets = active_summed_targets(topology)
    latest_by_target, stale_driver_count = _latest_current_driver_records(
        state.get("driver_measurements", []),
        driver_targets,
    )
    latest_summed_by_group, latest_summed_pairs_by_group, stale_summed_count = (
        _latest_current_summed_records(
            state.get("summed_validations", []),
            summed_targets,
        )
    )
    latest_summed_tests_by_group, stale_summed_test_count = (
        _latest_current_summed_tests(
            state.get("summed_tests", []),
            summed_targets,
        )
    )
    captured_targets = [
        target["target_id"]
        for target in driver_targets
        if latest_by_target.get(target["target_id"], {}).get("captured") is True
    ]
    missing_targets = [
        target for target in driver_targets
        if target["target_id"] not in captured_targets
    ]
    validated_groups: list[str] = []
    for target in summed_targets:
        group_id = target["speaker_group_id"]
        latest_test = latest_summed_tests_by_group.get(group_id, {})
        latest_validation = latest_summed_by_group.get(group_id, {})
        if latest_validation.get("validated") is not True:
            continue
        if not latest_test:
            continue
        if _record_playback_id(latest_validation) != _record_playback_id(latest_test):
            continue
        validated_groups.append(group_id)
    missing_summed = [
        target for target in summed_targets
        if target["speaker_group_id"] not in validated_groups
    ]
    measurements_complete = bool(driver_targets) and not missing_targets
    summed_complete = (
        measurements_complete
        and bool(summed_targets)
        and not missing_summed
    )
    return {
        "required_driver_count": len(driver_targets),
        "captured_driver_count": len(captured_targets),
        "missing_driver_targets": missing_targets,
        "driver_measurements_complete": measurements_complete,
        "required_driver_check_count": len(driver_targets),
        "captured_driver_check_count": len(captured_targets),
        "missing_driver_check_targets": missing_targets,
        "driver_checks_complete": measurements_complete,
        "required_summed_group_count": len(summed_targets),
        "validated_summed_group_count": len(validated_groups),
        "missing_summed_targets": missing_summed,
        "summed_validation_complete": summed_complete,
        "latest_driver_measurements": latest_by_target,
        "latest_driver_checks": latest_by_target,
        "latest_summed_tests": latest_summed_tests_by_group,
        "latest_summed_validations": latest_summed_by_group,
        "latest_summed_pairs_by_group": latest_summed_pairs_by_group,
        "stale_driver_record_count": stale_driver_count,
        "stale_summed_test_record_count": stale_summed_test_count,
        "stale_summed_record_count": stale_summed_count,
    }


def _with_summary(topology: OutputTopology, state: dict[str, Any]) -> dict[str, Any]:
    summary = _summarise(topology, state)
    issues: list[dict[str, str]] = []
    if not active_driver_targets(topology):
        issues.append(_issue(
            "warning",
            "active_driver_targets_missing",
            "saved output topology has no active crossover driver targets",
        ))
    for target in summary["missing_driver_targets"]:
        issues.append(_issue(
            "warning",
            "driver_measurement_missing",
            (
                f"measure {target['speaker_group_label']} "
                f"{target['role']} with a quiet test before saving an active baseline"
            ),
        ))
    for target in summary["missing_summed_targets"]:
        issues.append(_issue(
            "warning",
            "summed_validation_missing",
            (
                f"validate the summed crossover for "
                f"{target['speaker_group_label']} before saving an active baseline"
            ),
        ))
    if (
        summary["stale_driver_record_count"]
        or summary["stale_summed_test_record_count"]
        or summary["stale_summed_record_count"]
    ):
        issues.append(_issue(
            "warning",
            "stale_measurement_evidence_ignored",
            "previous measurement evidence no longer matches the saved speaker layout",
        ))
    if summary["summed_validation_complete"]:
        status = "ready_for_baseline"
    elif summary["driver_measurements_complete"]:
        status = "needs_summed_validation"
    elif summary["required_driver_count"]:
        status = "needs_driver_measurements"
    else:
        status = "not_applicable"
    out = dict(state)
    out.update({
        "status": status,
        "latest_by_target": summary["latest_driver_measurements"],
        "latest_summed_tests": summary["latest_summed_tests"],
        "latest_summed_by_group": summary["latest_summed_validations"],
        "latest_summed_pairs_by_group": summary["latest_summed_pairs_by_group"],
        "summary": summary,
        "issues": issues,
        "permissions": {
            "may_record_driver_measurement": True,
            "may_record_summed_validation": summary["driver_measurements_complete"],
            "may_compile_baseline": summary["summed_validation_complete"],
            "may_not_play_audio": True,
            "may_not_load_camilla": True,
        },
        "safety": {
            "no_audio": True,
            "loads_camilla": False,
            "applies_filters": False,
            "requires_mic_meter": False,
            "accepts_operator_listening_check": True,
            "requires_operator_confirmation": True,
        },
    })
    return out


def load_measurement_state(
    topology: OutputTopology,
    *,
    state_path: str | Path | None = None,
) -> dict[str, Any]:
    """Load measurement evidence and derive current readiness."""

    path = measurement_state_path(state_path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _with_summary(topology, _base_state(path))
    except (OSError, json.JSONDecodeError):
        state = _base_state(path)
        state["status"] = "unreadable"
        state["issues"] = [
            _issue(
                "blocker",
                "measurement_state_unreadable",
                "active speaker measurement state could not be read",
            )
        ]
        return state
    return _with_summary(topology, _normalise_state(raw, path))


def confirmed_driver_roles(
    topology: OutputTopology,
    *,
    speaker_group_id: str,
    state_path: str | Path | None = None,
) -> list[str]:
    """Return roles with current, captured driver-check evidence for a group."""

    group_id = str(speaker_group_id or "").strip()
    if not group_id:
        return []
    state = load_measurement_state(topology, state_path=state_path)
    summary = state.get("summary") if isinstance(state.get("summary"), Mapping) else {}
    latest = summary.get("latest_driver_measurements")
    if not isinstance(latest, Mapping):
        return []

    roles: list[str] = []
    seen: set[str] = set()
    for target in active_driver_targets(topology):
        if target.get("speaker_group_id") != group_id:
            continue
        record = latest.get(target.get("target_id"))
        if not isinstance(record, Mapping) or record.get("captured") is not True:
            continue
        role = str(target.get("role") or "").strip().lower()
        if role and role not in seen:
            roles.append(role)
            seen.add(role)
    return roles


def _write_state(path: Path, state: dict[str, Any]) -> None:
    atomic_write_text(
        path,
        json.dumps(state, indent=2, sort_keys=True) + "\n",
        mode=0o640,
    )


_DURABLE_REPEAT_SUMMARY_KEYS = (
    "repeat_group_id",
    "target",
    "accepted",
    "rejected",
    "recaptured",
    "needed_recapture",
    "aggregate",
    "spread_db_p90",
    "confidence",
    "admission_attempts",
)
_DURABLE_REPEAT_ENTRY_KEYS = (
    "index",
    "attempt",
    "verdict",
    "accepted",
    "reject_reason",
    "artifact_path",
    "estimated_snr_db",
    "clipping",
    "above_validity_floor",
    "level_dbfs",
)
_PROCESS_REPEAT_KEYS = frozenset({"aggregate_repeat"})


def _repeat_int(value: Any, field: str, *, minimum: int = 0) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= 4
    ):
        raise ValueError(
            f"repeat summary {field} must be an integer from {minimum} to 4"
        )
    return value


def _repeat_number(value: Any, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"repeat summary {field} must be numeric or null")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"repeat summary {field} must be finite")
    return result


def _repeat_text(
    value: Any, field: str, *, limit: int, optional: bool = False
) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"repeat summary {field} must be bounded text")
    return value


def _repeat_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"repeat summary {field} must be a boolean")
    return value


def _repeat_artifact_path(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, (str, Path)):
        raise ValueError("repeat summary artifact_path must be a path string or null")
    result = str(value)
    relative = PurePosixPath(result)
    if (
        not result
        or len(result) > 512
        or relative.is_absolute()
        or ".." in relative.parts
        or relative.as_posix() != result
    ):
        raise ValueError(
            "repeat summary artifact_path must be a canonical relative bundle path"
        )
    return result


def _durable_repeat_summary(raw: Any) -> dict[str, Any] | None:
    """Project process-local repeat aggregation onto its durable JSON schema."""

    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("repeat summary must be an object or null")
    required = (set(_DURABLE_REPEAT_SUMMARY_KEYS) - {"admission_attempts"}) | {
        "per_repeat"
    }
    allowed = required | {"admission_attempts"} | _PROCESS_REPEAT_KEYS
    if missing := required - set(raw):
        raise ValueError(f"repeat summary missing fields: {sorted(missing)}")
    if extra := set(raw) - allowed:
        raise ValueError(f"repeat summary has unsupported fields: {sorted(extra)}")

    entries = raw["per_repeat"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= 4:
        raise ValueError("repeat summary per_repeat must contain 1 to 4 entries")
    per_repeat = []
    for item in entries:
        if (
            not isinstance(item, Mapping)
            or set(item) != set(_DURABLE_REPEAT_ENTRY_KEYS)
        ):
            raise ValueError("repeat summary per_repeat entry schema is invalid")
        per_repeat.append({
            "index": _repeat_int(item["index"], "per_repeat.index"),
            "attempt": _repeat_int(
                item["attempt"], "per_repeat.attempt", minimum=1
            ),
            "verdict": _repeat_text(
                item["verdict"], "per_repeat.verdict", limit=80, optional=True
            ),
            "accepted": _repeat_bool(item["accepted"], "per_repeat.accepted"),
            "reject_reason": _repeat_text(
                item["reject_reason"],
                "per_repeat.reject_reason",
                limit=80,
                optional=True,
            ),
            "artifact_path": _repeat_artifact_path(item["artifact_path"]),
            "estimated_snr_db": _repeat_number(
                item["estimated_snr_db"], "per_repeat.estimated_snr_db"
            ),
            "clipping": _repeat_bool(item["clipping"], "per_repeat.clipping"),
            "above_validity_floor": _repeat_bool(
                item["above_validity_floor"], "per_repeat.above_validity_floor"
            ),
            "level_dbfs": _repeat_number(
                item["level_dbfs"], "per_repeat.level_dbfs"
            ),
        })

    summary = {
        "repeat_group_id": _repeat_text(
            raw["repeat_group_id"], "repeat_group_id", limit=120
        ),
        "target": _repeat_int(raw["target"], "target", minimum=1),
        "accepted": _repeat_int(raw["accepted"], "accepted"),
        "rejected": _repeat_int(raw["rejected"], "rejected"),
        "recaptured": _repeat_bool(raw["recaptured"], "recaptured"),
        "needed_recapture": _repeat_bool(
            raw["needed_recapture"], "needed_recapture"
        ),
        "aggregate": _repeat_text(raw["aggregate"], "aggregate", limit=40),
        "spread_db_p90": _repeat_number(
            raw["spread_db_p90"], "spread_db_p90"
        ),
        "confidence": _repeat_text(raw["confidence"], "confidence", limit=20),
        "per_repeat": per_repeat,
    }
    if "admission_attempts" in raw:
        summary["admission_attempts"] = _repeat_int(
            raw["admission_attempts"], "admission_attempts", minimum=1
        )
    return summary


def record_driver_measurement(
    topology: OutputTopology,
    raw: Mapping[str, Any],
    *,
    calibration_level: Mapping[str, Any] | None = None,
    safe_session: Mapping[str, Any] | None = None,
    durable_floor_confirmation: Mapping[str, Any] | None = None,
    bundle_ref: Mapping[str, Any] | None = None,
    state_path: str | Path | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Persist one per-driver quiet-test observation.

    A correct-driver operator result proves physical routing identity even
    when the browser has no usable microphone reading. Mic-backed response
    measurements are still captured when available and remain required for
    later acoustic tuning/validation steps.
    """

    path = measurement_state_path(state_path)
    state = load_measurement_state(topology, state_path=path)
    group_id = _text(raw.get("speaker_group_id"), max_chars=80) or ""
    role = (_text(raw.get("role"), max_chars=40) or "").lower()
    target_id = _target_id(group_id, role)
    target = _target_lookup(topology).get(target_id)
    outcome = (_text(raw.get("outcome"), max_chars=40) or "").lower()
    observed, clipping, meter = _mic_meter_from(raw, calibration_level)
    issues: list[dict[str, str]] = []
    if target is None:
        issues.append(_issue(
            "blocker",
            "driver_measurement_target_unknown",
            "driver measurement target is not in the saved output topology",
        ))
    if outcome not in DRIVER_OUTCOMES:
        issues.append(_issue(
            "blocker",
            "driver_measurement_outcome_invalid",
            "driver measurement outcome is unsupported",
        ))
    if target is not None and not target.get("identity_verified"):
        issues.append(_issue(
            "blocker",
            "driver_measurement_identity_unverified",
            "confirm this DAC output before recording it as measured",
        ))
    if target is not None and outcome == "heard_correct_driver":
        issues.extend(_floor_confirmation_issues(
            raw,
            target,
            safe_session,
            durable_floor_confirmation,
        ))
    if observed is None:
        issues.append(_issue(
            "warning",
            "driver_measurement_mic_missing",
            "no microphone reading was captured for acoustic tuning",
        ))
    if meter.get("status") in {"clipping", "too_loud"}:
        issues.append(_issue(
            "warning",
            "driver_measurement_mic_out_of_range",
            "microphone reading is too loud or clipping",
        ))
    captured = (
        not any(issue["severity"] == "blocker" for issue in issues)
        and outcome == "heard_correct_driver"
        and meter.get("status") not in {"clipping", "too_loud"}
    )
    record = {
        "measurement_id": uuid.uuid4().hex,
        "created_at": now or _utc_now(),
        "target_id": target_id,
        "target_fingerprint": target.get("target_fingerprint") if target else None,
        "speaker_group_id": group_id,
        "speaker_group_label": target.get("speaker_group_label") if target else None,
        "speaker_group_mode": target.get("speaker_group_mode") if target else None,
        "role": role,
        "output_index": target.get("output_index") if target else None,
        "output_label": target.get("output_label") if target else None,
        "outcome": outcome,
        "captured": captured,
        "observed_mic_dbfs": observed,
        "mic_clipping": clipping,
        "mic_meter": meter,
        # Optional mic-backed acoustic verdict block (driver_acoustics) when the
        # sweep+analyze commissioning path recorded this; None for the
        # operator-only quiet-test path.
        "acoustic": (
            dict(raw["acoustic"])
            if isinstance(raw.get("acoustic"), Mapping)
            else None
        ),
        "test_level_dbfs": _finite_float(raw.get("test_level_dbfs")),
        # Analyzer captures carry the complete generated-sweep + commissioning
        # gain ledger.  Operator-only floor checks leave this absent and can
        # prove routing, but can never be consumed as comparable acoustic level
        # evidence by the baseline compiler.
        "excitation": (
            dict(raw["excitation"])
            if isinstance(raw.get("excitation"), Mapping)
            else None
        ),
        # Server-normalized relay acknowledgement + comparison-set binding.
        # Operator-only and legacy acoustic records intentionally leave this
        # absent and cannot drive a new automatic crossover.
        "placement_proof": (
            dict(raw["placement_proof"])
            if isinstance(raw.get("placement_proof"), Mapping)
            else None
        ),
        "playback_id": _text(raw.get("playback_id"), max_chars=120),
        "floor_confirmation": dict(
            durable_floor_confirmation or _safe_floor_result(safe_session) or {}
        ),
        "notes": _text(raw.get("notes"), max_chars=1000),
        "issues": issues,
        # Optional durable-bundle join key ({session_id, artifact_path}) — see
        # jasper.active_speaker.bundles. Forensic only: never read back as an
        # input to any decision this module makes.
        "bundle": dict(bundle_ref) if isinstance(bundle_ref, Mapping) else None,
        # Optional three-repeat aggregate summary (SC-4 shape) when this
        # record is the outcome of commissioning_capture.aggregate_driver_repeats
        # rather than a single-shot capture. Per-repeat evidence beyond this
        # compact per_repeat[] summary (the full audio/curves) lives only in
        # the bundle's repeat_captures/ — this field never grows unbounded.
        "repeats": _durable_repeat_summary(raw.get("repeats")),
    }
    persisted = _normalise_state(state, path)
    persisted["driver_measurements"] = [
        *persisted.get("driver_measurements", []),
        record,
    ][-MAX_DRIVER_RECORDS:]
    persisted["updated_at"] = record["created_at"]
    out = _with_summary(topology, persisted)
    _write_state(path, out)
    return out


def record_summed_test_artifact(
    topology: OutputTopology,
    raw: Mapping[str, Any],
    *,
    state_path: str | Path | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Persist the combined-driver playback artifact/session used for validation."""

    path = measurement_state_path(state_path)
    state = load_measurement_state(topology, state_path=path)
    playback = raw.get("playback") if isinstance(raw.get("playback"), Mapping) else {}
    target = (
        playback.get("target")
        if isinstance(playback.get("target"), Mapping)
        else {}
    )
    group_id = (
        _text(raw.get("speaker_group_id"), max_chars=80)
        or _text(target.get("speaker_group_id"), max_chars=80)
        or ""
    )
    summed_target = _summed_lookup(topology).get(group_id)
    playback_id = _text(playback.get("playback_id"), max_chars=120)
    artifact = playback.get("artifact") if isinstance(playback.get("artifact"), Mapping) else {}
    stimulus = (
        playback.get("stimulus")
        if isinstance(playback.get("stimulus"), Mapping)
        else {}
    )
    expected_indices = _expected_summed_output_indices(topology, group_id)
    observed_indices = _output_indices_from_playback(playback)
    issues: list[dict[str, str]] = []
    playback_issues = [
        _issue(
            str(issue.get("severity") or "blocker"),
            str(issue.get("code") or "summed_test_playback_issue"),
            str(issue.get("message") or issue.get("code") or "combined test playback issue"),
        )
        for issue in (playback.get("issues") or [])
        if isinstance(issue, Mapping)
    ]
    issues.extend(playback_issues)
    if summed_target is None:
        issues.append(_issue(
            "blocker",
            "summed_test_group_unknown",
            "combined test target is not in the saved output topology",
        ))
    if not playback_id:
        issues.append(_issue(
            "blocker",
            "summed_test_playback_missing",
            "combined test did not produce a playback id",
        ))
    if playback.get("status") != "completed":
        issues.append(_issue(
            "blocker",
            "summed_test_playback_incomplete",
            "combined test did not complete",
        ))
    if not artifact:
        issues.append(_issue(
            "blocker",
            "summed_test_artifact_missing",
            "combined test did not produce an inspectable playback artifact",
        ))
    if artifact and expected_indices and observed_indices != expected_indices:
        issues.append(_issue(
            "blocker",
            "summed_test_output_mismatch",
            "combined test output channels do not match the saved speaker layout",
        ))
    captured = not any(issue["severity"] == "blocker" for issue in issues)
    record = {
        "summed_test_id": playback_id or uuid.uuid4().hex,
        "created_at": now or _utc_now(),
        "speaker_group_id": group_id,
        "group_fingerprint": (
            summed_target.get("group_fingerprint") if summed_target else None
        ),
        "captured": captured,
        "audio_emitted": bool(playback.get("audio_emitted")),
        "playback_id": playback_id,
        "backend": playback.get("backend"),
        "artifact": dict(artifact),
        "stimulus": dict(stimulus),
        "target_output_indices": observed_indices,
        "expected_output_indices": expected_indices,
        "tone": dict(playback.get("tone") or {}),
        "issues": issues,
    }
    persisted = _normalise_state(state, path)
    persisted["summed_tests"] = [
        *persisted.get("summed_tests", []),
        record,
    ][-MAX_SUMMED_TEST_RECORDS:]
    persisted["updated_at"] = record["created_at"]
    out = _with_summary(topology, persisted)
    _write_state(path, out)
    return out


def record_summed_validation(
    topology: OutputTopology,
    raw: Mapping[str, Any],
    *,
    calibration_level: Mapping[str, Any] | None = None,
    bundle_ref: Mapping[str, Any] | None = None,
    state_path: str | Path | None = None,
    driver_target_proof_complete: bool = False,
    now: str | None = None,
) -> dict[str, Any]:
    """Persist one summed crossover validation observation.

    ``bundle_ref``, when supplied, is stored verbatim on the record as
    ``bundle`` — the same ``{session_id, artifact_path}`` join key
    :func:`record_driver_measurement` stores. See its docstring.

    ``raw["region"]``, when supplied, is the crossover region this capture
    belongs to (``{"lower_role", "upper_role", "fc_hz"}`` —
    ``commissioning_capture.record_summed_acoustic_capture`` resolves it from
    the preset at the analyzed fc). Validated through :func:`_valid_region`
    and stored verbatim as ``region``, or ``None`` when absent or malformed —
    never a half-formed region a pair reader could misfile. See
    :func:`_latest_current_summed_records` for how region + polarity
    (``acoustic.expect_null``) combine into paired evidence.
    """

    path = measurement_state_path(state_path)
    state = load_measurement_state(topology, state_path=path)
    group_id = _text(raw.get("speaker_group_id"), max_chars=80) or ""
    outcome = (_text(raw.get("outcome"), max_chars=40) or "").lower()
    operator_listening_check = _truthy_flag(raw.get("operator_listening_check"))
    observed, clipping, meter = _mic_meter_from(raw, calibration_level)
    summary = state.get("summary") if isinstance(state.get("summary"), dict) else {}
    summed_target = _summed_lookup(topology).get(group_id)
    latest_tests = (
        summary.get("latest_summed_tests")
        if isinstance(summary.get("latest_summed_tests"), Mapping)
        else {}
    )
    latest_test = latest_tests.get(group_id) if isinstance(latest_tests, Mapping) else None
    requested_test_id = (
        _text(raw.get("summed_test_id"), max_chars=120)
        or _text(raw.get("playback_id"), max_chars=120)
    )
    issues: list[dict[str, str]] = []
    if summed_target is None:
        issues.append(_issue(
            "blocker",
            "summed_validation_group_unknown",
            "summed validation target is not in the saved output topology",
        ))
    if outcome not in SUMMED_OUTCOMES:
        issues.append(_issue(
            "blocker",
            "summed_validation_outcome_invalid",
            "summed validation outcome is unsupported",
        ))
    if (
        not summary.get("driver_measurements_complete")
        and not driver_target_proof_complete
    ):
        issues.append(_issue(
            "blocker",
            "summed_validation_driver_measurements_missing",
            "measure each driver before validating the summed crossover",
        ))
    if not isinstance(latest_test, Mapping) or not latest_test.get("captured"):
        issues.append(_issue(
            "blocker",
            "summed_validation_test_missing",
            "run a combined-driver test before recording whether the crossover blends",
        ))
    elif not requested_test_id:
        issues.append(_issue(
            "blocker",
            "summed_validation_test_id_missing",
            "combined crossover validation must reference the latest combined test",
        ))
    elif requested_test_id not in {
        str(latest_test.get("summed_test_id") or ""),
        str(latest_test.get("playback_id") or ""),
    }:
        issues.append(_issue(
            "blocker",
            "summed_validation_test_stale",
            "run the combined-driver test again before recording this result",
        ))
    elif latest_test.get("audio_emitted") is not True:
        issues.append(_issue(
            "blocker",
            "summed_validation_audio_missing",
            "combined crossover validation requires an audible combined-driver test",
        ))
    if observed is None:
        issues.append(_issue(
            "warning",
            "summed_validation_mic_missing",
            "no microphone reading was captured for acoustic tuning",
        ))
    if meter.get("status") in {"clipping", "too_loud"}:
        issues.append(_issue(
            "warning",
            "summed_validation_mic_out_of_range",
            "microphone reading is too loud or clipping",
        ))
    validated = (
        not any(issue["severity"] == "blocker" for issue in issues)
        and outcome == "blend_ok"
        and (operator_listening_check or observed is not None)
        and meter.get("status") not in {"clipping", "too_loud"}
    )
    record = {
        "validation_id": uuid.uuid4().hex,
        "created_at": now or _utc_now(),
        "speaker_group_id": group_id,
        "group_fingerprint": (
            summed_target.get("group_fingerprint") if summed_target else None
        ),
        "outcome": outcome,
        "validated": validated,
        "operator_listening_check": operator_listening_check,
        "summed_test_id": requested_test_id,
        "summed_test": dict(latest_test) if isinstance(latest_test, Mapping) else {},
        "driver_target_proof_complete": bool(driver_target_proof_complete),
        "observed_mic_dbfs": observed,
        "mic_clipping": clipping,
        "mic_meter": meter,
        # Optional mic-backed summed-crossover verdict block (driver_acoustics)
        # when the sweep+analyze path recorded this; None for the operator path.
        "acoustic": (
            dict(raw["acoustic"])
            if isinstance(raw.get("acoustic"), Mapping)
            else None
        ),
        "excitation": (
            dict(raw["excitation"])
            if isinstance(raw.get("excitation"), Mapping)
            else None
        ),
        "placement_proof": (
            dict(raw["placement_proof"])
            if isinstance(raw.get("placement_proof"), Mapping)
            else None
        ),
        "polarity": _text(raw.get("polarity"), max_chars=40) or "normal",
        "delay_ms": _finite_float(raw.get("delay_ms")),
        "delay_target_role": (
            _text(raw.get("delay_target_role"), max_chars=40) or None
        ),
        "notes": _text(raw.get("notes"), max_chars=1000),
        "issues": issues,
        "bundle": dict(bundle_ref) if isinstance(bundle_ref, Mapping) else None,
        "region": _valid_region(raw.get("region")),
    }
    persisted = _normalise_state(state, path)
    persisted["summed_validations"] = [
        *persisted.get("summed_validations", []),
        record,
    ][-MAX_SUMMED_RECORDS:]
    persisted["updated_at"] = record["created_at"]
    out = _with_summary(topology, persisted)
    _write_state(path, out)
    return out
