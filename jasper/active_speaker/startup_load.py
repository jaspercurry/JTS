# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Guarded active-speaker startup config load, rollback, and anchor re-emit.

This is the first active-speaker slice that may reload CamillaDSP.
`load_protected_startup_config`/`rollback_protected_startup_config` still
do not play tones, touch normal listening volume, or authorize playback —
they keep the side-effect boundary deliberately small: validate the staged
muted/protected startup candidate, require path-safety evidence, load through
the existing DSP apply lifecycle, and persist a rollback target.

The per-driver/summed commissioning lifecycle, which swaps the RUNNING graph,
lives in `commission_load`.
"""

from __future__ import annotations

import json
import logging
import math
import stat
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, NamedTuple

from jasper.atomic_io import atomic_write_json
from jasper.control.restart_broker import manage_units
from jasper.dsp_apply import (
    CamillaConfigValidationResult,
    DspApplyError,
    apply_dsp_config,
    validate_camilla_config,
)
from jasper.output_topology import OutputTopology, channel_identity_report

from ._common import gate as _gate, issue as _issue
from .calibration_level import (
    MIN_TEST_LEVEL_DBFS,
    load_calibration_level_state,
)
from .environment import (
    classify_camilla_config_text,
    read_camilla_statefile_config_path,
)
from .path_safety import (
    evaluate_path_safety_evidence,
    software_guard_ready_for_startup,
    staged_target_signature,
    target_assignment_signature,
    topology_target_signature,
    validate_startup_load_evidence_binding,
)
from .startup_hold import (
    hold_staged_startup,
    release_staged_startup_hold,
    startup_hold_marker_path,
)
from .runtime_contract import (
    GRAPH_ALL_MUTED_ACTIVE_STARTUP,
    safe_graph_for_current_topology,
)
from .safe_playback import load_safe_playback_state
from .state_paths import baseline_profile_state_path, startup_load_state_path
from .staging import load_staged_startup_config

logger = logging.getLogger(__name__)

STARTUP_LOAD_SCHEMA_VERSION = 1
STARTUP_LOAD_PREFLIGHT_KIND = "jts_active_speaker_startup_load_preflight"
STARTUP_LOAD_STATE_KIND = "jts_active_speaker_startup_load_state"
AUDIO_HARDWARE_RECONCILE_UNIT = "jasper-audio-hardware-reconcile.service"

PathLoader = Callable[[str], Awaitable[bool]]
ConfigPathReader = Callable[[], Awaitable[str | None]]


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _normalise_issue(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return _issue("warning", "unknown_issue", "unknown issue")
    return _issue(
        str(raw.get("severity") or "warning"),
        str(raw.get("code") or "unknown_issue"),
        str(raw.get("message") or raw.get("code") or "unknown issue"),
    )


def _base_load_state(
    path: Path,
    *,
    schema_version: int,
    kind: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the shared persisted state shape for guarded graph loads."""

    state: dict[str, Any] = {
        "artifact_schema_version": schema_version,
        "kind": kind,
        "status": "idle",
        "state_path": str(path),
        "updated_at": _utc_now(),
        "loaded": False,
        "candidate_config_path": None,
        "active_config_path": None,
        "previous_config_path": None,
        "rollback_available": False,
        "last_action": "status",
    }
    state.update(extra or {})
    state["issues"] = []
    return state


def _base_state(path: Path) -> dict[str, Any]:
    return _base_load_state(
        path,
        schema_version=STARTUP_LOAD_SCHEMA_VERSION,
        kind=STARTUP_LOAD_STATE_KIND,
    )


def load_startup_load_state(
    *,
    state_path: str | Path | None = None,
) -> dict[str, Any]:
    """Return the latest active-speaker load/rollback state."""

    path = startup_load_state_path(state_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _base_state(path)
    if not isinstance(payload, dict):
        return _base_state(path)
    state = _base_state(path)
    state.update(payload)
    state["state_path"] = str(path)
    state["loaded"] = state.get("status") == "loaded"
    state["rollback_available"] = bool(
        state.get("loaded") and state.get("previous_config_path")
    )
    state["issues"] = [
        _normalise_issue(issue)
        for issue in state.get("issues", [])
        if isinstance(issue, dict)
    ]
    return state


def _record_state(
    payload: dict[str, Any],
    *,
    state_path: str | Path | None = None,
) -> None:
    path = startup_load_state_path(state_path)
    payload = dict(payload)
    payload["state_path"] = str(path)
    payload["updated_at"] = payload.get("updated_at") or _utc_now()
    atomic_write_json(
        path,
        payload,
        mode=0o640,
        group_from_parent=True,
    )


def _trigger_audio_hardware_reconcile(*, source: str) -> bool:
    """Ask PID 1 to reconcile outputd after Camilla graph transitions.

    Active outputd activation is gated on both hardware presence and the
    active Camilla graph. Hardware events already trigger this unit via udev;
    startup load/rollback are the matching graph events.
    """

    result = manage_units(
        AUDIO_HARDWARE_RECONCILE_UNIT,
        verb="start",
        reason=source,
        # Wait for the oneshot here. The next commissioning step may play a
        # tone immediately, and outputd must already be reading the active lane.
        no_block=False,
        timeout=15.0,
    )
    if not result.get("ok"):
        logger.warning(
            "event=active_speaker.audio_hardware_reconcile_trigger_failed source=%s unit=%s error=%s",
            source,
            AUDIO_HARDWARE_RECONCILE_UNIT,
            result.get("error") or f"rc={result.get('rc')}",
        )
        return False
    logger.info(
        "event=active_speaker.audio_hardware_reconcile_triggered source=%s unit=%s",
        source,
        AUDIO_HARDWARE_RECONCILE_UNIT,
    )
    return True


def _level_value(calibration_level: dict[str, Any], key: str, default: float) -> float:
    raw = calibration_level.get("test_signal") or {}
    try:
        value = float(raw.get(key))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def _calibration_at_floor(calibration_level: dict[str, Any]) -> bool:
    requested = _level_value(
        calibration_level,
        "requested_level_dbfs",
        MIN_TEST_LEVEL_DBFS,
    )
    floor = _level_value(calibration_level, "min_level_dbfs", MIN_TEST_LEVEL_DBFS)
    return requested <= floor + 1e-6


def _topology_blockers(
    topology: OutputTopology,
    *,
    software_guard_ready: bool,
) -> list[dict[str, str]]:
    ignored = {"tweeter_software_guard_requested"} if software_guard_ready else set()
    return [
        _normalise_issue(issue)
        for issue in topology.evaluation().get("blockers", [])
        if isinstance(issue, dict) and str(issue.get("code")) not in ignored
    ]


def _staged_config_path(staged_config: dict[str, Any]) -> Path | None:
    config = staged_config.get("config") if isinstance(staged_config, dict) else None
    if not isinstance(config, dict):
        return None
    raw = config.get("path")
    return Path(raw) if isinstance(raw, str) and raw.strip() else None


def _staged_topology_payload(
    topology: OutputTopology,
    staged_config: dict[str, Any],
    *,
    require_physical_identity: bool = True,
) -> dict[str, Any]:
    """Return whether staged metadata still matches the saved topology."""

    if staged_config.get("status") != "staged":
        return {
            "status": "not_staged",
            "matched": False,
            "issues": [],
        }
    issues: list[dict[str, str]] = []
    staged_topology = (
        staged_config.get("topology")
        if isinstance(staged_config.get("topology"), dict)
        else {}
    )
    staged_hardware = (
        staged_config.get("hardware")
        if isinstance(staged_config.get("hardware"), dict)
        else {}
    )
    staged_signature = staged_target_signature(staged_config)
    topology_signature = topology_target_signature(topology)
    if not require_physical_identity:
        staged_signature = target_assignment_signature(staged_signature)
        topology_signature = target_assignment_signature(topology_signature)
    checks = {
        "topology_id": staged_topology.get("topology_id") == topology.topology_id,
        "hardware_device": staged_hardware.get("device_id") == topology.hardware.device_id,
        "hardware_card": staged_hardware.get("card_id") == topology.hardware.card_id,
        "hardware_output_count": (
            staged_hardware.get("physical_output_count")
            == topology.hardware.physical_output_count
        ),
        "hardware_clock_domain": (
            staged_hardware.get("clock_domain_id") == topology.hardware.clock_domain_id
        ),
        "targets": staged_signature == topology_signature,
    }
    for check, passed in checks.items():
        if not passed:
            issues.append(
                _issue(
                    "blocker",
                    f"staged_{check}_mismatch",
                    (
                        "staged protected startup config no longer matches "
                        f"the saved output topology: {check}"
                    ),
                )
            )
    matched = not issues
    return {
        "status": "matched" if matched else "mismatch",
        "matched": matched,
        "checks": checks,
        "issues": issues,
    }


def staged_topology_match_status(
    topology: OutputTopology,
    staged_config: dict[str, Any],
    *,
    require_physical_identity: bool = True,
) -> dict[str, Any]:
    """Return whether staged startup metadata still matches saved topology."""

    return _staged_topology_payload(
        topology,
        staged_config,
        require_physical_identity=require_physical_identity,
    )


def _candidate_payload(
    path: Path | None,
    *,
    validate: Callable[[str | Path], CamillaConfigValidationResult],
) -> dict[str, Any]:
    if path is None:
        return {
            "path": None,
            "exists": False,
            "classification": "missing",
            "validation": {"status": "skipped", "reason": "no_config_path"},
            "issues": [
                _issue(
                    "blocker",
                    "startup_config_path_missing",
                    "staged startup config does not include a config path",
                )
            ],
        }
    issues: list[dict[str, str]] = []
    payload: dict[str, Any] = {
        "path": str(path),
        "basename": path.name,
        "exists": path.exists(),
        "classification": "missing",
        "validation": {"status": "skipped", "reason": "not_readable"},
        "issues": issues,
    }
    if not path.exists():
        issues.append(
            _issue(
                "blocker",
                "startup_config_missing",
                f"staged startup config does not exist: {path}",
            )
        )
        return payload
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        issues.append(
            _issue(
                "blocker",
                "startup_config_unreadable",
                f"could not read staged startup config: {type(exc).__name__}",
            )
        )
        return payload
    classification = classify_camilla_config_text(text)
    payload.update({
        "classification": classification.get("classification"),
        "label": classification.get("label"),
        "playback_device": classification.get("playback_device"),
        "playback_channels": classification.get("playback_channels"),
        "volume_limit_db": classification.get("volume_limit_db"),
        "volume_limit_ok": bool(classification.get("volume_limit_ok")),
        "active_split": classification.get("active_split"),
    })
    issues.extend(_normalise_issue(issue) for issue in classification.get("issues", []))
    validation = validate(path).to_dict()
    payload["validation"] = validation
    if validation.get("status") != "valid":
        issues.append(
            _issue(
                "blocker",
                "startup_config_validation_not_valid",
                (
                    "staged startup config must pass camilladsp --check before load; "
                    f"validation status is {validation.get('status') or 'unknown'}"
                ),
            )
        )
    if classification.get("classification") != "active_startup_candidate":
        issues.append(
            _issue(
                "blocker",
                "active_startup_candidate_required",
                "staged config is not a JTS active-speaker startup candidate",
            )
        )
    return payload


def _path_safety_payload(path: str | Path | None) -> dict[str, Any]:
    if path is None:
        return {
            "provided": False,
            "status": "missing",
            "ok_to_load_active_config": False,
            "load_gate": "evidence_missing",
            "issues": [
                _issue(
                    "blocker",
                    "path_safety_evidence_missing",
                    "active-speaker path-safety evidence was not provided",
                )
            ],
        }
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        report = evaluate_path_safety_evidence(raw)
    except (OSError, ValueError) as exc:
        return {
            "provided": True,
            "path": str(path),
            "status": "invalid",
            "ok_to_load_active_config": False,
            "load_gate": "evidence_invalid",
            "issues": [
                _issue(
                    "blocker",
                    "path_safety_evidence_invalid",
                    f"active-speaker path-safety evidence is invalid: {type(exc).__name__}",
                )
            ],
        }
    report["provided"] = True
    report["path"] = str(path)
    report["evidence_mode"] = raw.get("evidence_mode")
    report["scope"] = raw.get("scope")
    report["provenance"] = (
        raw.get("provenance") if isinstance(raw.get("provenance"), dict) else {}
    )
    report["raw_evidence"] = raw
    report["issues"] = [
        _normalise_issue(issue)
        for issue in report.get("issues", [])
        if isinstance(issue, dict)
    ]
    return report


def _tone_playback_idle(safe_session: dict[str, Any]) -> bool:
    playback = safe_session.get("playback") if isinstance(safe_session, dict) else {}
    if not isinstance(playback, dict):
        return True
    status = str(playback.get("status") or "idle")
    return status not in {"starting", "playing", "running", "in_progress"}


def build_startup_load_preflight(
    topology: OutputTopology,
    *,
    staged_config: dict[str, Any] | None = None,
    calibration_level: dict[str, Any] | None = None,
    safe_session: dict[str, Any] | None = None,
    path_safety_evidence_path: str | Path | None = None,
    current_config_path: str | Path | None = None,
    require_physical_identity: bool = True,
    validate: Callable[[str | Path], CamillaConfigValidationResult] = (
        validate_camilla_config
    ),
) -> dict[str, Any]:
    """Return the deterministic preflight for loading the protected config."""

    staged = staged_config if isinstance(staged_config, dict) else load_staged_startup_config()
    level = (
        calibration_level
        if isinstance(calibration_level, dict)
        else load_calibration_level_state()
    )
    session = (
        safe_session if isinstance(safe_session, dict) else load_safe_playback_state()
    )
    staged_path = _staged_config_path(staged)
    candidate = _candidate_payload(staged_path, validate=validate)
    staged_topology = _staged_topology_payload(
        topology,
        staged,
        require_physical_identity=require_physical_identity,
    )
    path_safety = _path_safety_payload(path_safety_evidence_path)
    if isinstance(path_safety.get("raw_evidence"), dict):
        path_safety_binding = validate_startup_load_evidence_binding(
            path_safety["raw_evidence"],
            topology,
            staged_config=staged,
            current_config_path=current_config_path,
            require_physical_identity=require_physical_identity,
        )
    else:
        path_safety_binding = {
            "status": "missing",
            "matched": False,
            "checks": {},
            "issues": [],
        }
    path_safety_ok = bool(path_safety.get("ok_to_load_active_config"))
    path_safety_bound = bool(path_safety_binding.get("matched"))
    path_safety_load_gate = str(path_safety.get("load_gate") or "blocked")
    if path_safety_ok and not path_safety_bound:
        path_safety_load_gate = "evidence_stale"
    identity = channel_identity_report(topology)
    software_guard_ready = software_guard_ready_for_startup(topology, staged)
    topology_blockers = _topology_blockers(
        topology,
        software_guard_ready=software_guard_ready,
    )
    assigned = int(identity.get("assigned_channel_count") or 0)
    unverified = int(identity.get("unverified_channel_count") or 0)
    level_at_floor = _calibration_at_floor(level)
    playback_idle = _tone_playback_idle(session)
    candidate_blockers = [
        issue
        for issue in candidate.get("issues", [])
        if issue.get("severity") == "blocker"
    ]
    physical_identity_verified = assigned > 0 and (
        unverified == 0 if require_physical_identity else True
    )
    gates = [
        _gate(
            "staged_config_ready",
            label="Protected startup config is staged",
            passed=staged.get("status") == "staged" and staged_path is not None,
            message=(
                "Protected startup config is staged"
                if staged.get("status") == "staged" and staged_path is not None
                else "Stage the protected startup config first"
            ),
        ),
        _gate(
            "candidate_validated",
            label="Staged config is a validated active-speaker startup candidate",
            passed=not candidate_blockers,
            message=(
                "Staged startup config is validated"
                if not candidate_blockers
                else "Resolve staged config validation blockers"
            ),
        ),
        _gate(
            "staged_topology_matches_current",
            label="Staged config still matches the saved output topology",
            passed=bool(staged_topology.get("matched")),
            message=(
                "Staged protected config matches the saved topology"
                if staged_topology.get("matched")
                else "Restage the protected config after output setup changes"
            ),
        ),
        _gate(
            "topology_has_no_unhandled_blockers",
            label="Saved output topology has no unhandled blockers",
            passed=not topology_blockers,
            message=(
                "Saved output topology is usable for startup load"
                if not topology_blockers
                else "Resolve saved output topology blockers"
            ),
        ),
        _gate(
            "physical_identity_verified",
            label="Assigned physical outputs are verified",
            passed=physical_identity_verified,
            message=(
                "Physical output identity is verified"
                if physical_identity_verified
                else (
                    "Assign DAC outputs before loading active DSP"
                    if not require_physical_identity
                    else "Verify assigned DAC outputs before loading active DSP"
                )
            ),
        ),
        _gate(
            "software_guard_ready",
            label="High-frequency guard evidence is ready",
            passed=software_guard_ready,
            message=(
                "Software-guarded startup evidence is ready"
                if software_guard_ready
                else "Stage and inspect the software-guarded startup config"
            ),
        ),
        _gate(
            "path_safety_ready",
            label="Path safety evidence authorizes active config load",
            passed=path_safety_ok,
            message=(
                "Hardware-probe-backed path safety is ready"
                if path_safety_ok
                else f"Path safety gate is {path_safety_load_gate}"
            ),
        ),
        _gate(
            "path_safety_matches_current_startup_load",
            label="Path safety evidence matches this startup load",
            passed=path_safety_bound,
            message=(
                "Path safety evidence matches this startup load"
                if path_safety_bound
                else "Run Check protected path again before loading"
            ),
        ),
        _gate(
            "calibration_level_at_floor",
            label="Calibration level is at the floor",
            passed=level_at_floor,
            message=(
                "Calibration level is at the floor"
                if level_at_floor
                else "Reset calibration level before loading the active startup config"
            ),
        ),
        _gate(
            "no_active_tone_playback",
            label="No tone playback is active",
            passed=playback_idle,
            message=(
                "No active tone playback is running"
                if playback_idle
                else "Stop tone playback before loading the active startup config"
            ),
        ),
    ]
    issues = list(topology_blockers)
    issues.extend(
        _normalise_issue(issue)
        for issue in candidate.get("issues", [])
        if isinstance(issue, dict)
    )
    issues.extend(
        _normalise_issue(issue)
        for issue in staged_topology.get("issues", [])
        if isinstance(issue, dict)
    )
    issues.extend(_normalise_issue(issue) for issue in path_safety.get("issues", []))
    issues.extend(
        _normalise_issue(issue)
        for issue in path_safety_binding.get("issues", [])
        if isinstance(issue, dict)
    )
    if not level_at_floor:
        issues.append(
            _issue(
                "blocker",
                "calibration_level_not_at_floor",
                "calibration level must be reset to the floor before startup load",
            )
        )
    if not playback_idle:
        issues.append(
            _issue(
                "blocker",
                "tone_playback_active",
                "tone playback must be stopped before startup load",
            )
        )
    blocker_count = sum(1 for issue in issues if issue.get("severity") == "blocker")
    ready = blocker_count == 0 and all(gate["passed"] for gate in gates)
    return {
        "artifact_schema_version": STARTUP_LOAD_SCHEMA_VERSION,
        "kind": STARTUP_LOAD_PREFLIGHT_KIND,
        "status": "ready" if ready else "blocked",
        "load_allowed": ready,
        "candidate": candidate,
        "staged_topology": {
            "status": staged_topology.get("status"),
            "matched": bool(staged_topology.get("matched")),
            "checks": staged_topology.get("checks") or {},
        },
        "path_safety": {
            "status": path_safety.get("status"),
            "load_gate": path_safety_load_gate,
            "ok_to_load_active_config": path_safety_ok and path_safety_bound,
            "evidence_ok": path_safety_ok,
            "binding": {
                "status": path_safety_binding.get("status"),
                "matched": path_safety_bound,
                "checks": path_safety_binding.get("checks") or {},
            },
            "path": path_safety.get("path"),
        },
        "identity": {
            "status": identity.get("status"),
            "assigned_channel_count": assigned,
            "unverified_channel_count": unverified,
            "physical_identity_required": require_physical_identity,
        },
        "calibration_level": {
            "requested_level_dbfs": _level_value(
                level,
                "requested_level_dbfs",
                MIN_TEST_LEVEL_DBFS,
            ),
            "at_floor": level_at_floor,
        },
        "safe_session": {
            "status": session.get("status"),
            "playback_status": (session.get("playback") or {}).get("status"),
        },
        "required_gates": gates,
        "issues": issues,
        "next_step": (
            "Ready to load the protected startup config. This will not play sound."
            if ready
            else "Resolve startup load blockers before reloading CamillaDSP."
        ),
    }


def _loaded_state_payload(
    *,
    status: str,
    candidate_config_path: str | None,
    active_config_path: str | None,
    previous_config_path: str | None,
    last_action: str,
    preflight: dict[str, Any] | None = None,
    dsp_apply: dict[str, Any] | None = None,
    issues: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    loaded = status == "loaded"
    return {
        "artifact_schema_version": STARTUP_LOAD_SCHEMA_VERSION,
        "kind": STARTUP_LOAD_STATE_KIND,
        "status": status,
        "updated_at": _utc_now(),
        "loaded": loaded,
        "candidate_config_path": candidate_config_path,
        "active_config_path": active_config_path,
        "previous_config_path": previous_config_path,
        "rollback_available": bool(loaded and previous_config_path),
        "last_action": last_action,
        "preflight_status": (preflight or {}).get("status"),
        "path_safety_load_gate": ((preflight or {}).get("path_safety") or {}).get(
            "load_gate"
        ),
        "dsp_apply": dsp_apply,
        "issues": issues or [],
    }


async def load_protected_startup_config(
    topology: OutputTopology,
    *,
    load_config: PathLoader,
    get_current_config_path: ConfigPathReader,
    path_safety_evidence_path: str | Path | None = None,
    state_path: str | Path | None = None,
    require_physical_identity: bool = True,
    validate: Callable[[str | Path], CamillaConfigValidationResult] = (
        validate_camilla_config
    ),
) -> dict[str, Any]:
    """Load the staged active-speaker startup config after all gates pass."""

    try:
        prior_config_path = await get_current_config_path()
    except Exception as exc:  # noqa: BLE001
        preflight = build_startup_load_preflight(
            topology,
            path_safety_evidence_path=path_safety_evidence_path,
            require_physical_identity=require_physical_identity,
            validate=validate,
        )
        candidate_path = preflight.get("candidate", {}).get("path")
        issue = _issue(
            "blocker",
            "current_config_snapshot_failed",
            f"could not read current CamillaDSP config path: {type(exc).__name__}",
        )
        payload = _loaded_state_payload(
            status="failed",
            candidate_config_path=candidate_path,
            active_config_path=None,
            previous_config_path=None,
            last_action="load_failed",
            preflight=preflight,
            issues=[issue],
        )
        _record_state(payload, state_path=state_path)
        return {"preflight": preflight, "load": payload}

    preflight = build_startup_load_preflight(
        topology,
        path_safety_evidence_path=path_safety_evidence_path,
        current_config_path=prior_config_path,
        require_physical_identity=require_physical_identity,
        validate=validate,
    )
    candidate_path = preflight.get("candidate", {}).get("path")
    if not preflight.get("load_allowed"):
        payload = _loaded_state_payload(
            status="blocked",
            candidate_config_path=candidate_path,
            active_config_path=None,
            previous_config_path=str(prior_config_path) if prior_config_path else None,
            last_action="load_blocked",
            preflight=preflight,
            issues=[
                _normalise_issue(issue)
                for issue in preflight.get("issues", [])
                if isinstance(issue, dict)
            ],
        )
        _record_state(payload, state_path=state_path)
        logger.info(
            "event=active_speaker.startup_load result=blocked blockers=%d gate=%s",
            len(payload["issues"]),
            preflight.get("path_safety", {}).get("load_gate"),
        )
        return {"preflight": preflight, "load": payload}

    if not prior_config_path:
        issue = _issue(
            "blocker",
            "current_config_snapshot_missing",
            "CamillaDSP did not report a current config path for rollback",
        )
        payload = _loaded_state_payload(
            status="failed",
            candidate_config_path=candidate_path,
            active_config_path=None,
            previous_config_path=None,
            last_action="load_failed",
            preflight=preflight,
            issues=[issue],
        )
        _record_state(payload, state_path=state_path)
        return {"preflight": preflight, "load": payload}
    if not Path(str(prior_config_path)).exists():
        issue = _issue(
            "blocker",
            "rollback_anchor_missing",
            f"current CamillaDSP config path does not exist: {prior_config_path}",
        )
        payload = _loaded_state_payload(
            status="blocked",
            candidate_config_path=candidate_path,
            active_config_path=None,
            previous_config_path=str(prior_config_path),
            last_action="load_blocked",
            preflight=preflight,
            issues=[issue],
        )
        _record_state(payload, state_path=state_path)
        logger.info(
            "event=active_speaker.startup_load result=blocked reason=rollback_anchor_missing prior=%s",
            prior_config_path,
        )
        return {"preflight": preflight, "load": payload}

    # Hold the staged anchor BEFORE touching the DSP. The reconcile this load
    # kicks re-runs the graph selector, which restores the saved baseline over
    # the anchor unless the hold is present (safe_graph_for_current_topology's
    # deadlock-guard rung reads this marker). A load that cannot be held would
    # have its durable half undone seconds later, so refuse here rather than
    # answer success: nothing has been applied yet at this point.
    if not hold_staged_startup():
        hold_marker = startup_hold_marker_path()
        payload = _loaded_state_payload(
            status="blocked",
            candidate_config_path=candidate_path,
            active_config_path=None,
            previous_config_path=str(prior_config_path),
            last_action="load_blocked",
            preflight=preflight,
            issues=[
                _issue(
                    "blocker",
                    "staged_startup_hold_unavailable",
                    "could not hold the staged startup anchor at "
                    f"{hold_marker}: the reconcile this load kicks would "
                    "restore the saved baseline over it. The writing service "
                    "needs write access to that directory, which a sandboxed "
                    "unit gets from RuntimeDirectory=jasper-active-speaker.",
                )
            ],
        )
        _record_state(payload, state_path=state_path)
        logger.warning(
            "event=active_speaker.startup_load result=blocked "
            "reason=staged_startup_hold_unavailable marker=%s",
            hold_marker,
        )
        return {"preflight": preflight, "load": payload}

    def _persist_loaded_anchor() -> None:
        _record_state(
            _loaded_state_payload(
                status="loaded",
                candidate_config_path=candidate_path,
                active_config_path=candidate_path,
                previous_config_path=str(prior_config_path),
                last_action="load",
                preflight=preflight,
            ),
            state_path=state_path,
        )

    # The hold is taken before the apply, so EVERY way out of the apply that
    # leaves the anchor off the durable statefile has to give it back — not just
    # the one this function renders a payload for. `apply_dsp_config` raises at
    # least two non-`DspApplyError` types on the writer-lock path both web
    # surfaces contend for (`DspWriterLockTimeout`, `BassExtensionApplyPending`),
    # and an awaited call can also be cancelled. A `finally` covers all of them
    # without a broad `except`, and it re-raises nothing, so the caller's error
    # handling stays exactly as it was.
    apply_succeeded = False
    try:
        apply_state = await apply_dsp_config(
            source="active_speaker_startup_load",
            candidate_path=str(candidate_path),
            prior_config_path=str(prior_config_path),
            load_config=load_config,
            get_current_config_path=get_current_config_path,
            persist=_persist_loaded_anchor,
            validate=validate,
        )
        apply_succeeded = True
    except DspApplyError as exc:
        payload = _loaded_state_payload(
            status="failed",
            candidate_config_path=candidate_path,
            active_config_path=None,
            previous_config_path=str(prior_config_path),
            last_action="load_failed",
            preflight=preflight,
            dsp_apply=exc.state.to_dict(),
            issues=[
                _issue(
                    "blocker",
                    "startup_config_load_failed",
                    f"CamillaDSP startup load failed: {exc}",
                )
            ],
        )
        _record_state(payload, state_path=state_path)
        logger.warning(
            "event=active_speaker.startup_load result=failed candidate=%s prior=%s error=%s",
            candidate_path,
            prior_config_path,
            type(exc).__name__,
        )
        return {"preflight": preflight, "load": payload}
    finally:
        # Runs on the return above too, so the rolled-back apply gives the hold
        # back by the same one line that covers the escapes this function never
        # sees. The success path clears the flag, so a held anchor stays held.
        if not apply_succeeded:
            release_staged_startup_hold()

    payload = _loaded_state_payload(
        status="loaded",
        candidate_config_path=str(candidate_path),
        active_config_path=apply_state.active_config_path or str(candidate_path),
        previous_config_path=apply_state.prior_config_path or str(prior_config_path),
        last_action="load",
        preflight=preflight,
        dsp_apply=apply_state.to_dict(),
    )
    _record_state(payload, state_path=state_path)
    # The hold taken before the apply is still in force, so the reconcile kicked
    # here preserves the anchor it just wrote instead of restoring the baseline.
    _trigger_audio_hardware_reconcile(source="active_speaker_startup_load")
    logger.info(
        "event=active_speaker.startup_load result=loaded candidate=%s prior=%s op_id=%s",
        payload["candidate_config_path"],
        payload["previous_config_path"],
        apply_state.op_id,
    )
    return {"preflight": preflight, "load": payload}


async def rollback_protected_startup_config(
    *,
    load_config: PathLoader,
    get_current_config_path: ConfigPathReader,
    state_path: str | Path | None = None,
    validate: Callable[[str | Path], CamillaConfigValidationResult] = (
        validate_camilla_config
    ),
) -> dict[str, Any]:
    """Reload the config that was active before the protected startup load."""

    current_state = load_startup_load_state(state_path=state_path)
    previous = current_state.get("previous_config_path")
    if current_state.get("status") != "loaded" or not previous:
        issue = _issue(
            "blocker",
            "startup_rollback_unavailable",
            "no loaded active-speaker startup config has a rollback target",
        )
        payload = _loaded_state_payload(
            status="blocked",
            candidate_config_path=current_state.get("candidate_config_path"),
            active_config_path=current_state.get("active_config_path"),
            previous_config_path=previous,
            last_action="rollback_blocked",
            issues=[issue],
        )
        return {"rollback": payload}
    if not Path(str(previous)).exists():
        issue = _issue(
            "blocker",
            "rollback_config_missing",
            f"rollback config no longer exists: {previous}",
        )
        payload = _loaded_state_payload(
            status="rollback_failed",
            candidate_config_path=current_state.get("candidate_config_path"),
            active_config_path=current_state.get("active_config_path"),
            previous_config_path=str(previous),
            last_action="rollback_failed",
            issues=[issue],
        )
        _record_state(payload, state_path=state_path)
        return {"rollback": payload}

    try:
        active_before = await get_current_config_path()
        apply_state = await apply_dsp_config(
            source="active_speaker_startup_rollback",
            candidate_path=str(previous),
            prior_config_path=active_before,
            load_config=load_config,
            get_current_config_path=get_current_config_path,
            validate=validate,
        )
    except Exception as exc:  # noqa: BLE001
        dsp_state = exc.state.to_dict() if isinstance(exc, DspApplyError) else None
        payload = _loaded_state_payload(
            status="rollback_failed",
            candidate_config_path=current_state.get("candidate_config_path"),
            active_config_path=current_state.get("active_config_path"),
            previous_config_path=str(previous),
            last_action="rollback_failed",
            dsp_apply=dsp_state,
            issues=[
                _issue(
                    "blocker",
                    "startup_rollback_failed",
                    f"CamillaDSP rollback failed: {exc}",
                )
            ],
        )
        _record_state(payload, state_path=state_path)
        logger.warning(
            "event=active_speaker.startup_rollback result=failed target=%s error=%s",
            previous,
            type(exc).__name__,
        )
        return {"rollback": payload}

    payload = _loaded_state_payload(
        status="rolled_back",
        candidate_config_path=current_state.get("candidate_config_path"),
        active_config_path=apply_state.active_config_path or str(previous),
        previous_config_path=str(previous),
        last_action="rollback",
        dsp_apply=apply_state.to_dict(),
    )
    _record_state(payload, state_path=state_path)
    # The staged anchor was abandoned (the statefile is back on the previous
    # graph), so the startup-load hold no longer applies. Cleared BEFORE the
    # reconcile kick so this rollback's own reconcile restores the baseline
    # rather than re-preserving the anchor. Best-effort — a failed clear never
    # fails the rollback, and the marker is ephemeral (/run) either way.
    release_staged_startup_hold()
    _trigger_audio_hardware_reconcile(source="active_speaker_startup_rollback")
    logger.info(
        "event=active_speaker.startup_rollback result=rolled_back target=%s op_id=%s",
        previous,
        apply_state.op_id,
    )
    return {"rollback": payload}


def startup_anchor_from_decision(decision: Any) -> Any | None:
    """The decision's operative graph, when it IS the all-muted startup anchor.

    Two safe-graph statuses can put a box on that anchor, and they differ only in
    where the graph came from: ``preserve_current`` (the loaded graph already is
    it) and ``select_active_startup`` (the persisted staged candidate is it). The
    classification is checked rather than the status alone, because
    ``preserve_current`` is also how an APPROVED runtime graph and a
    driver-domain baseline are preserved — keying on the status would let this
    command re-stage over a box that is on neither.
    """
    graph = {
        "preserve_current": decision.current_graph,
        "select_active_startup": decision.fallback_graph,
    }.get(decision.status)
    if (
        graph is not None
        and graph.allowed
        and graph.classification == GRAPH_ALL_MUTED_ACTIVE_STARTUP
    ):
        return graph
    return None


def describe_safe_graph_for_refusal(decision: Any) -> str:
    """What this box was actually found on, in one line an operator can act on."""
    seen = [
        f"{label}={graph.classification}"
        for label, graph in (
            ("current", decision.current_graph),
            ("preferred", decision.preferred_graph),
            ("fallback", decision.fallback_graph),
        )
        if graph is not None
    ]
    detail = f"safe-graph status={decision.status}"
    if seen:
        detail += " (" + ", ".join(seen) + ")"
    return detail


def _relocate_validation_evidence(
    validation: Any, proof_path: Path, target: Path
) -> Any:
    """Re-point a validation result's own path fields from the proof dir to the target.

    ``validate_camilla_config`` records the file it was handed — ``path`` on
    every return branch, and the same string inside ``argv`` when the camilladsp
    binary exists. The anchor is proved in a temporary directory that is deleted
    before the metadata lands, so publishing the result verbatim would record a
    path that cannot exist. The VERDICT is about bytes that are now at ``target``,
    so the verdict travels and its locations are corrected with it.

    Substring replacement over ``argv`` rather than a rebuilt command line: the
    argv belongs to the validator, and guessing its shape here would be a second
    place that knows how it invokes camilladsp.
    """
    if not isinstance(validation, Mapping):
        return validation
    proof, dest = str(proof_path), str(target)
    relocated = dict(validation)
    if relocated.get("path") == proof:
        relocated["path"] = dest
    argv = relocated.get("argv")
    if isinstance(argv, list):
        relocated["argv"] = [
            dest if arg == proof else arg for arg in argv
        ]
    return relocated


#: Every way the re-emit can decline. Closed so the renderer stays exhaustive.
ReemitAnchorRefusal = Literal[
    "commission_load_active", "stage_failed", "reproof_failed",
    "out_parent_missing", "lock_contended",
]


class ReemitAnchorReport(NamedTuple):
    """What one startup-anchor re-emit did. The CLI owns how these facts print."""

    device: str
    source: str
    reason: ReemitAnchorRefusal | None = None  # None: the anchor was re-emitted
    classification: str | None = None
    written_path: Path | None = None
    preview: bool = False
    statefile_written: bool = False
    byte_count: int = 0
    detail: str | None = None  # the refusing branch's one extra fact
    issues: tuple[Mapping[str, Any], ...] = ()
    active_target: str | None = None
    candidate_config_path: str | None = None


def reemit_staged_startup_anchor(
    topology: Any,
    *,
    device: str,
    source: str,
    statefile: str | Path,
    applied_baseline_state: str | Path | None = None,
    out: str | Path | None = None,
    force: bool = False,
) -> ReemitAnchorReport:
    """Re-stage the all-muted startup ANCHOR against ``device``. Step 1, no baseline.

    The fleet-typical composite box is mid-commission by design: it has no
    APPLIED baseline, and its boot graph is the all-muted staged startup graph.
    Its ring arm needs the same first step every roleful box needs — the GRAPH
    moves first, so ``jasper-audio-hardware-reconcile`` has a loaded graph to
    derive the endpoint marker FROM.

    DERIVED FROM PERSISTED STATE ONLY. The re-stage reads the box's own saved
    design draft and crossover preview — the same two files
    ``jasper.active_speaker.web_commissioning._stage_startup_config`` reads when
    it is handed neither a preset nor a preview. The operator supplies exactly
    one thing, ``--endpoint``, which is the act that breaks the marker<->graph
    fixed point; nothing else about the graph is operator-supplied.

    NOTHING LIVE IS TOUCHED UNTIL THE GRAPH PROVES. The staged artifact sits at a
    FIXED path, so writing it IS moving the boot graph — there is no separate
    pointer to gate on. So the re-stage runs against a temporary directory
    first, with the real CamillaDSP validation, and only the exact bytes that
    proved are published over the live artifact. A refusal leaves the box's
    existing anchor untouched, which mirrors the applied path's "a refusal
    writes nothing at all".

    SINGLE-FLIGHT AGAINST A LIVE COMMISSION LOAD. The path this publishes over is
    not only the boot graph — it is the universal RE-MUTE anchor of the audible
    commissioning flow. ``load_driver_commissioning_config`` records it as
    ``previous_config_path``, and four controls reload exactly it: the
    commission-load rollback, ``commission-rollback``, ``commission-ramp abort``,
    and the operator's own by-ear ``commission-ramp ack --outcome too_loud``.
    Moving it to a different endpoint while a driver is armed at level would
    re-point the operator's stop button at a graph whose device this box may not
    be arming yet. So this refuses while a commission load is active, mirroring
    the refusal ``_cmd_commission_load`` already makes for the same shared
    artifact; ``--force`` is the same escape hatch there. Roll back first.

    A STALE ``loaded`` RECORD REFUSES TOO, and that is expected rather than a
    bug to route around. The state file is durable
    (``/var/lib/jasper/active_speaker_commission_load.json``) while per-driver
    commissioning is deliberately transient, so a reboot or a CamillaDSP restart
    mid-commission leaves ``status="loaded"`` on disk with nothing loaded. This
    reads that record RAW — no live-graph consult — because the whole point of
    the command is to work with CamillaDSP down, and
    ``commission_load_runtime_status`` (what ``commission-rollback`` and the web
    wizard overlay to report ``stale``) needs the running graph to answer. The
    way past a stale record is ``--force``, or a ``commission-rollback`` /
    wizard visit, which reconcile the record against the live graph.
    """
    import tempfile

    # Deferred: commission_load imports this module at module scope.
    from jasper.active_speaker.commission_load import load_commission_load_state
    from jasper.active_speaker.crossover_preview import load_crossover_preview
    from jasper.active_speaker.design_draft import load_design_draft
    from jasper.active_speaker.runtime_contract import write_camilla_statefile
    from jasper.active_speaker.staging import (
        StagedAnchorLockContended,
        stage_protected_startup_config,
        staged_anchor_lock,
        staged_config_path,
        staged_metadata_path,
    )
    from jasper.atomic_io import atomic_write_json, atomic_write_text

    # Single-flight (see SINGLE-FLIGHT above). Checked BEFORE the stage, so a
    # refused run does no work and touches nothing at all.
    existing = load_commission_load_state()
    if existing.get("status") == "loaded" and not force:
        return ReemitAnchorReport(
            device, source, "commission_load_active",
            active_target=existing.get("target"),
            candidate_config_path=existing.get("candidate_config_path"),
        )

    design_draft = load_design_draft()
    crossover_preview = load_crossover_preview(current_design_draft=design_draft)

    with tempfile.TemporaryDirectory(prefix="jts-reemit-anchor-") as tmp:
        proof_dir = Path(tmp)
        payload = stage_protected_startup_config(
            topology,
            crossover_preview=crossover_preview,
            playback_device=device,
            config_dir=proof_dir,
            metadata_path=proof_dir / "staged_metadata.json",
        )
        blockers = [
            issue
            for issue in payload.get("issues") or []
            if isinstance(issue, Mapping) and issue.get("severity") == "blocker"
        ]
        if payload.get("status") != "staged" or blockers:
            return ReemitAnchorReport(
                device, source, "stage_failed", issues=tuple(blockers)
            )

        proof_path = Path(payload["config"]["path"])
        yaml = proof_path.read_text(encoding="utf-8")

        # RE-PROOF before any byte lands, exactly as the applied path re-proves —
        # and through the SAME host that will select this graph at the next
        # deploy or CamillaDSP restart. Asking the selector rather than the
        # classifier directly is what makes the answer mean "this box will boot
        # from it", not merely "these bytes classify". It also keeps
        # `persisted_candidate` classification owned by one function
        # (`tests/test_active_speaker_cli.py` pins that), instead of teaching a
        # second caller the evidence-pairing rules.
        #
        # The proof graph goes in as `current_config_path` so the selector judges
        # THESE bytes. Left to its default it would read the statefile — the box's
        # existing anchor — and happily preserve the very graph being replaced.
        # The staged metadata is the proof run's own, so the graph is judged
        # against the evidence that describes it rather than the outgoing set.
        proof_decision = safe_graph_for_current_topology(
            topology,
            current_config_path=proof_path,
            applied_baseline_path=baseline_profile_state_path(applied_baseline_state),
            staged_metadata_path=Path(payload["metadata_path"]),
            # There is no applied baseline on this path by definition; saying so
            # keeps a missing-baseline read out of the decision entirely.
            consider_applied_baseline=False,
        )
        graph = startup_anchor_from_decision(proof_decision)
        selected = proof_decision.selected_config_path
        if graph is None or selected != str(proof_path):
            return ReemitAnchorReport(
                device, source, "reproof_failed",
                detail=describe_safe_graph_for_refusal(proof_decision),
                issues=tuple(proof_decision.issues),
            )

        if out:
            preview_path = Path(out)
            if not preview_path.parent.exists():
                return ReemitAnchorReport(
                    device, source, "out_parent_missing",
                    detail=str(preview_path.parent),
                )
            atomic_write_text(preview_path, yaml, mode=0o640)
            return ReemitAnchorReport(
                device, source, classification=graph.classification,
                written_path=preview_path, preview=True, byte_count=len(yaml),
            )

        # Publish the PROVEN bytes. YAML first, then the metadata that locates
        # it. Both orders are recoverable — the anchor's path is FIXED, so
        # metadata written first would locate the previous, still-valid bytes
        # rather than nothing — but this order keeps the interleaved window
        # pointing at a graph whose evidence is at worst one revision stale,
        # instead of at bytes its evidence has never described.
        #
        # Second writer of this pair; the first is the /sound wizard's own
        # staging (`stage_protected_startup_config`). Both halves go out under
        # the pair's shared `staged_anchor_lock`, so an interleaved run can no
        # longer leave one graph's metadata over another's bytes (#2518). The
        # proof run above deliberately stays outside the hold: it writes only
        # into its temp directory, and holding the live lock across a full
        # re-stage plus CamillaDSP validation would make a /sound/ save wait on
        # work that publishes nothing. A /sound/ stage that lands between the
        # proof and this publish is therefore overwritten wholesale — last pair
        # wins, and each pair stays internally consistent, which is the property
        # #2518 asks for.
        target = staged_config_path()
        meta_target = staged_metadata_path()
        try:
            with staged_anchor_lock(target, source="baseline-reemit"):
                try:
                    target_mode = stat.S_IMODE(target.stat().st_mode)
                except OSError:
                    target_mode = 0o640
                atomic_write_text(
                    target,
                    yaml,
                    mode=target_mode,
                    group_from_parent=True,
                    durable=True,
                )
                # Every field that names a LOCATION is rewritten — the three
                # top-level ones and the validation result's own `path`/`argv`,
                # which `validate_camilla_config` builds from whatever file it
                # was handed and which would otherwise publish the deleted proof
                # directory. Nothing reads those two today
                # (`_staged_candidate_ready` takes only `status`), so this is
                # evidence honesty rather than behaviour — but published
                # evidence naming a path that cannot exist is exactly the kind
                # of thing a later reader trusts. Every remaining field
                # describes the graph itself and is location-independent, so the
                # published evidence stays the evidence that proved.
                published = dict(payload)
                published["metadata_path"] = str(meta_target)
                published["config"] = {
                    **payload["config"],
                    "path": str(target),
                    "basename": target.name,
                    "validation": _relocate_validation_evidence(
                        payload["config"].get("validation"), proof_path, target
                    ),
                }
                # durable=True to match the graph write above: this file is what
                # LOCATES the graph, so losing it to a power cut while the
                # durable half survives leaves the box off its anchor until
                # someone re-stages. It parks silent and still takes deploys,
                # but the two halves should survive together.
                atomic_write_json(
                    meta_target,
                    published,
                    mode=0o640,
                    group_from_parent=True,
                    durable=True,
                )
        except StagedAnchorLockContended as exc:
            return ReemitAnchorReport(
                device, source, "lock_contended", detail=str(exc)
            )

        statefile_path = Path(statefile)
        statefile_written = False
        if read_camilla_statefile_config_path(statefile_path) != str(target):
            write_camilla_statefile(statefile_path, target)
            statefile_written = True

        return ReemitAnchorReport(
            device, source, classification=graph.classification,
            written_path=target, statefile_written=statefile_written,
            byte_count=len(yaml),
        )
