"""Durable active-speaker measurement evidence.

This module records the evidence produced by the guided active-crossover flow:
one measured result per driver and one summed crossover validation per active
speaker group. It does not play tones, capture audio, load CamillaDSP, or infer
acoustic truth from thin evidence. It stores what the UI and operator observed
so the baseline compiler can decide whether it has enough evidence to proceed.
"""

from __future__ import annotations

import json
import hashlib
import math
import os
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from jasper.atomic_io import atomic_write_text
from jasper.output_topology import OutputTopology

from .calibration_level import classify_mic_meter
from .safe_playback import playback_target_signature

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


def _issue(severity: str, code: str, message: str) -> dict[str, str]:
    return {"severity": severity, "code": code, "message": message}


def measurement_state_path(path: str | Path | None = None) -> Path:
    return Path(path or os.environ.get(STATE_PATH_ENV) or DEFAULT_STATE_PATH)


def _finite_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


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
) -> list[dict[str, str]]:
    playback_id = _text(raw.get("playback_id"), max_chars=120)
    result = _safe_floor_result(safe_session)
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
    if not isinstance(safe_session, Mapping) or safe_session.get("status") != "armed":
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


def _summarise(topology: OutputTopology, state: dict[str, Any]) -> dict[str, Any]:
    driver_targets = active_driver_targets(topology)
    summed_targets = active_summed_targets(topology)
    latest_by_target, stale_driver_count = _latest_current_driver_records(
        state.get("driver_measurements", []),
        driver_targets,
    )
    latest_summed_by_group, stale_summed_count = _latest_current_summed_records(
        state.get("summed_validations", []),
        summed_targets,
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
    validated_groups = [
        target["speaker_group_id"]
        for target in summed_targets
        if latest_summed_by_group.get(
            target["speaker_group_id"],
            {},
        ).get("validated") is True
    ]
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
        "required_summed_group_count": len(summed_targets),
        "validated_summed_group_count": len(validated_groups),
        "missing_summed_targets": missing_summed,
        "summed_validation_complete": summed_complete,
        "latest_driver_measurements": latest_by_target,
        "latest_summed_tests": latest_summed_tests_by_group,
        "latest_summed_validations": latest_summed_by_group,
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
                f"{target['role']} before saving an active baseline"
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
            "requires_mic_meter": True,
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


def _write_state(path: Path, state: dict[str, Any]) -> None:
    atomic_write_text(
        path,
        json.dumps(state, indent=2, sort_keys=True) + "\n",
        mode=0o640,
    )


def record_driver_measurement(
    topology: OutputTopology,
    raw: Mapping[str, Any],
    *,
    calibration_level: Mapping[str, Any] | None = None,
    safe_session: Mapping[str, Any] | None = None,
    state_path: str | Path | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Persist one per-driver measurement observation."""

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
        issues.extend(_floor_confirmation_issues(raw, target, safe_session))
    if observed is None:
        issues.append(_issue(
            "warning",
            "driver_measurement_mic_missing",
            "record a microphone reading before this counts as measured",
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
        and observed is not None
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
        "test_level_dbfs": _finite_float(raw.get("test_level_dbfs")),
        "playback_id": _text(raw.get("playback_id"), max_chars=120),
        "floor_confirmation": dict(_safe_floor_result(safe_session) or {}),
        "notes": _text(raw.get("notes"), max_chars=1000),
        "issues": issues,
    }
    persisted = _normalise_state(state, path)
    persisted["driver_measurements"] = [
        *persisted.get("driver_measurements", []),
        record,
    ][-MAX_DRIVER_RECORDS:]
    persisted["updated_at"] = record["created_at"]
    _write_state(path, persisted)
    return _with_summary(topology, persisted)


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
    expected_indices = _expected_summed_output_indices(topology, group_id)
    observed_indices = _output_indices_from_playback(playback)
    issues: list[dict[str, str]] = []
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
    if expected_indices and observed_indices != expected_indices:
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
    _write_state(path, persisted)
    return _with_summary(topology, persisted)


def record_summed_validation(
    topology: OutputTopology,
    raw: Mapping[str, Any],
    *,
    calibration_level: Mapping[str, Any] | None = None,
    state_path: str | Path | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Persist one summed crossover validation observation."""

    path = measurement_state_path(state_path)
    state = load_measurement_state(topology, state_path=path)
    group_id = _text(raw.get("speaker_group_id"), max_chars=80) or ""
    outcome = (_text(raw.get("outcome"), max_chars=40) or "").lower()
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
    if not summary.get("driver_measurements_complete"):
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
            "record a microphone reading before this counts as validated",
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
        and observed is not None
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
        "summed_test_id": requested_test_id,
        "summed_test": dict(latest_test) if isinstance(latest_test, Mapping) else {},
        "observed_mic_dbfs": observed,
        "mic_clipping": clipping,
        "mic_meter": meter,
        "polarity": _text(raw.get("polarity"), max_chars=40) or "normal",
        "delay_ms": _finite_float(raw.get("delay_ms")),
        "delay_target_role": (
            _text(raw.get("delay_target_role"), max_chars=40) or None
        ),
        "notes": _text(raw.get("notes"), max_chars=1000),
        "issues": issues,
    }
    persisted = _normalise_state(state, path)
    persisted["summed_validations"] = [
        *persisted.get("summed_validations", []),
        record,
    ][-MAX_SUMMED_RECORDS:]
    persisted["updated_at"] = record["created_at"]
    _write_state(path, persisted)
    return _with_summary(topology, persisted)
