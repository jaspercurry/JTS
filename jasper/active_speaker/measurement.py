# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Durable active-speaker driver-check and measurement evidence."""

from __future__ import annotations

import json
import hashlib
import logging
import math
import os
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from jasper.atomic_io import atomic_write_text
from jasper.json_fields import utc_now_iso as _utc_now
from jasper.log_event import log_event
from jasper.output_topology import (
    OutputTopology,
    physical_target_id as _target_id,
    main_speaker_groups,
    topology_is_subless_passive_mains,
)

from ._common import (
    finite_float as _finite_float,
    issue as _issue,
)
from .calibration_level import classify_mic_meter
from .capture_geometry import REFERENCE_AXIS_DRIVER_PLACEMENT_POLICY_ID
from .repeat_admission import MAX_RESERVATIONS
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
MAX_DRIVER_RECORDS = 48


def measurement_state_path(path: str | Path | None = None) -> Path:
    return Path(path or os.environ.get(STATE_PATH_ENV) or DEFAULT_STATE_PATH)


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


def _crossover_groups(topology: OutputTopology) -> list[Any]:
    """Return active two-way and three-way speaker groups."""
    return [
        group for group in topology.speaker_groups
        if group.mode in {"active_2_way", "active_3_way"}
    ]


def measured_speaker_groups(topology: OutputTopology) -> list[Any]:
    """The groups whose drivers need per-driver measurement evidence.

    Every crossover group, PLUS a subless passive main, whose one full-range
    driver a recommissioning session measures with one routed solo and so needs
    the same target, safety limits and ceilings. Passive mains WITH a sub are
    bass management, not this session.
    """
    groups = _crossover_groups(topology)
    if topology_is_subless_passive_mains(topology):
        groups = groups + main_speaker_groups(topology)
    return groups


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
        **({"output_variant": target["output_variant"]} if target.get("output_variant", "primary") != "primary" else {}),
        "output_index": target.get("output_index"),
    })


def physical_driver_target(
    topology: OutputTopology,
    group: Any,
    channel: Any,
) -> dict[str, Any]:
    """Describe and fingerprint one physical driver channel.

    Eligibility remains the owning workflow's decision: measurement calls this
    only for active groups, while driver research may also describe a passive
    full-range component. Keeping construction here gives both workflows one
    target-identity contract without widening measurement eligibility.
    """

    target = {
        "target_id": channel.target_id(group.id),
        **({"output_variant": channel.output_variant} if channel.output_variant != "primary" else {}),
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
    }
    target["target_fingerprint"] = _target_fingerprint(topology, target)
    return target


def _driver_targets_for(
    topology: OutputTopology, groups: Sequence[Any],
) -> list[dict[str, Any]]:
    """Every channel of ``groups`` as a fingerprinted driver target.

    Shared so the two eligibility filters above are the only difference.
    """
    return [
        physical_driver_target(topology, group, channel)
        for group in groups
        for channel in group.channels
    ]


def active_driver_targets(topology: OutputTopology) -> list[dict[str, Any]]:
    """Return the driver targets that need measurement evidence.

    Eligibility is :func:`measured_speaker_groups`, WIDER than "has a crossover".
    """

    return _driver_targets_for(topology, measured_speaker_groups(topology))


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
    """Return crossover group targets with roles and fingerprints."""

    crossover_groups = _crossover_groups(topology)
    # NOT ``active_driver_targets``: that set is WIDER, and the fingerprint
    # below only ever consumed the crossover groups' own targets.
    driver_targets = _driver_targets_for(topology, crossover_groups)
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
        for group in crossover_groups
    ]


def _target_lookup(topology: OutputTopology) -> dict[str, dict[str, Any]]:
    return {target["target_id"]: target for target in active_driver_targets(topology)}


def _group_ids(topology: OutputTopology) -> set[str]:
    return {group.id for group in _crossover_groups(topology)}


def _base_state(path: Path) -> dict[str, Any]:
    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": MEASUREMENT_STATE_KIND,
        "status": "not_started",
        "updated_at": None,
        "state_path": str(path),
        "driver_measurements": [],
        "latest_by_target": {},
        "latest_reference_axis_by_target": {},
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
    return state


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


def _latest_current_driver_records(
    records: list[dict[str, Any]],
    targets: list[dict[str, Any]],
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    int,
]:
    """Return current driver evidence in geometry-scoped latest indexes.

    ``latest_by_target`` remains the near-field/legacy level-trim surface.
    Fixed-axis captures are deliberately separate so recording one cannot
    shadow the near-field response that baseline level matching consumes.
    """

    target_by_id = {target["target_id"]: target for target in targets}
    latest_near_field: dict[str, dict[str, Any]] = {}
    latest_reference_axis: dict[str, dict[str, Any]] = {}
    stale_count = 0
    for record in reversed(records):
        target_id = record.get("target_id")
        if not isinstance(target_id, str) or target_id not in target_by_id:
            continue
        target = target_by_id[target_id]
        if record.get("target_fingerprint") == target.get("target_fingerprint"):
            acoustic = record.get("acoustic")
            acoustic_geometry = (
                acoustic.get("capture_geometry")
                if isinstance(acoustic, Mapping)
                else None
            )
            proof = record.get("placement_proof")
            proof_policy = (
                proof.get("policy_id") if isinstance(proof, Mapping) else None
            )
            is_reference_axis = bool(
                acoustic_geometry == "reference_axis"
                or proof_policy == REFERENCE_AXIS_DRIVER_PLACEMENT_POLICY_ID
            )
            index = latest_reference_axis if is_reference_axis else latest_near_field
            index.setdefault(target_id, record)
        else:
            stale_count += 1
    return latest_near_field, latest_reference_axis, stale_count


def _latest_current_driver_confirmations(
    records: list[dict[str, Any]],
    targets: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Latest by-ear driver confirmation per target, immune to sweep evidence.

    Records with an ``acoustic`` block are sweep evidence, not operator floor
    confirmations. A newer sweep record must not replace a valid confirmation
    with a different playback id.
    """
    target_by_id = {target["target_id"]: target for target in targets}
    latest: dict[str, dict[str, Any]] = {}
    for record in reversed(records):
        target_id = record.get("target_id")
        if not isinstance(target_id, str) or target_id not in target_by_id:
            continue
        if isinstance(record.get("acoustic"), Mapping):
            continue
        target = target_by_id[target_id]
        if record.get("target_fingerprint") != target.get("target_fingerprint"):
            continue
        latest.setdefault(target_id, record)
    return latest


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
        "output_variant": target.get("output_variant", "primary"),
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
    output_variant: str = "primary",
) -> dict[str, Any]:
    """Validate durable identity/floor evidence from a current-state summary.

    The summary normally excludes stale records, but this authorization boundary
    independently resolves the current topology target and compares every
    identity field before trusting the embedded confirmation.
    """
    group_id = str(speaker_group_id or "").strip()
    role_id = str(role or "").strip().lower()
    target_id = _target_id(group_id, role_id, output_variant)
    target = _target_lookup(topology).get(target_id)
    summary = measurements.get("summary")
    # Confirmation-only latest -- never `latest_driver_measurements`, which is
    # newest-record-wins across BOTH the by-ear confirmation and sweep-evidence
    # writers. Recording sweep evidence must not be able to invalidate the
    # operator's confirmation here; see `_latest_current_driver_confirmations`.
    latest = summary.get("latest_driver_confirmations") if isinstance(summary, Mapping) else None
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
    (
        latest_by_target,
        latest_reference_axis_by_target,
        stale_driver_count,
    ) = _latest_current_driver_records(
        state.get("driver_measurements", []),
        driver_targets,
    )
    latest_driver_confirmations_by_target = _latest_current_driver_confirmations(
        state.get("driver_measurements", []),
        driver_targets,
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
    measurements_complete = bool(driver_targets) and not missing_targets
    return {
        "required_driver_count": len(driver_targets),
        "captured_driver_count": len(captured_targets),
        "missing_driver_targets": missing_targets,
        "driver_measurements_complete": measurements_complete,
        "required_driver_check_count": len(driver_targets),
        "captured_driver_check_count": len(captured_targets),
        "missing_driver_check_targets": missing_targets,
        "driver_checks_complete": measurements_complete,
        "latest_driver_measurements": latest_by_target,
        "latest_driver_checks": latest_by_target,
        "latest_reference_axis_driver_measurements": (
            latest_reference_axis_by_target
        ),
        # Confirmation-kind-only view of the same records -- what
        # `current_driver_floor_evidence` validates. Never read this as the
        # acoustic/level-trim surface; use `latest_driver_measurements` for
        # that (it intentionally still reflects whichever record is newest,
        # sweep evidence included).
        "latest_driver_confirmations": latest_driver_confirmations_by_target,
        "stale_driver_record_count": stale_driver_count,
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
    if summary["stale_driver_record_count"]:
        issues.append(_issue(
            "warning",
            "stale_measurement_evidence_ignored",
            "previous measurement evidence no longer matches the saved speaker layout",
        ))
    if summary["driver_measurements_complete"]:
        status = "ready_for_baseline"
    elif summary["required_driver_count"]:
        status = "needs_driver_measurements"
    else:
        status = "not_applicable"
    out = dict(state)
    out.update({
        "status": status,
        "latest_by_target": summary["latest_driver_measurements"],
        "latest_reference_axis_by_target": summary[
            "latest_reference_axis_driver_measurements"
        ],
        "summary": summary,
        "issues": issues,
        "permissions": {
            "may_record_driver_measurement": True,
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
    # Confirmation-only latest, same reasoning as `current_driver_floor_evidence`:
    # this reports which roles the operator has confirmed by ear, and must not
    # flip to "unconfirmed" just because a later sweep capture recorded its own
    # (differently-gated) evidence for the same target.
    latest = summary.get("latest_driver_confirmations")
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
    "capture_admission",
)
_PROCESS_REPEAT_KEYS = frozenset({"aggregate_repeat"})


def _repeat_int(
    value: Any, field: str, *, minimum: int = 0, maximum: int = 4
) -> int:
    # ``maximum`` defaults to the audible ``MAX_ATTEMPTS`` budget (4), the
    # ceiling on accept/reject/target counts and per_repeat length. Only the
    # raw reservation ``per_repeat.attempt`` VALUE rides higher — a set that
    # survived refunded transport failures reaches its third accept at a
    # reservation number up to ``MAX_RESERVATIONS`` — so that one field passes
    # the wider ceiling explicitly.
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ValueError(
            f"repeat summary {field} must be an integer from {minimum} to {maximum}"
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


def _repeat_optional_bool(value: Any, field: str) -> bool | None:
    if value is None:
        return None
    return _repeat_bool(value, field)


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
                item["attempt"],
                "per_repeat.attempt",
                minimum=1,
                maximum=MAX_RESERVATIONS,
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
            "above_validity_floor": _repeat_optional_bool(
                item["above_validity_floor"], "per_repeat.above_validity_floor"
            ),
            "level_dbfs": _repeat_number(
                item["level_dbfs"], "per_repeat.level_dbfs"
            ),
            "capture_admission": (
                dict(item["capture_admission"])
                if isinstance(item["capture_admission"], Mapping)
                else None
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
    capture_admission: Mapping[str, Any] | None = None,
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
    target_id = _target_id(group_id, role, str(raw.get("output_variant", "primary")))
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
        # Server-normalized acknowledgement + comparison-set binding.
        # Operator-only and legacy acoustic records intentionally leave this
        # absent and cannot drive a new automatic crossover.
        "placement_proof": (
            dict(raw["placement_proof"])
            if isinstance(raw.get("placement_proof"), Mapping)
            else None
        ),
        "playback_id": _text(raw.get("playback_id"), max_chars=120),
        # Exact production excitation authority. Legacy/direct captures leave
        # this absent and remain diagnostic-only.
        "capture_admission": (
            dict(capture_admission)
            if isinstance(capture_admission, Mapping)
            else None
        ),
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
