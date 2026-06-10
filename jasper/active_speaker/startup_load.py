"""Guarded active-speaker startup config load and rollback.

This is the first active-speaker slice that may reload CamillaDSP. It still
does not play tones, touch normal listening volume, or authorize playback. The
module keeps the side-effect boundary deliberately small: validate the staged
muted/protected startup candidate, require path-safety evidence, load through
the existing DSP apply lifecycle, and persist a rollback target.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from jasper.dsp_apply import (
    CamillaConfigValidationResult,
    DspApplyError,
    apply_dsp_config,
    validate_camilla_config,
)
from jasper.output_topology import OutputTopology, channel_identity_report

from .calibration_level import (
    MIN_TEST_LEVEL_DBFS,
    load_calibration_level_state,
)
from .environment import classify_camilla_config_text
from .path_safety import (
    evaluate_path_safety_evidence,
    software_guard_ready_for_startup,
    staged_target_signature,
    topology_target_signature,
    validate_startup_load_evidence_binding,
)
from .safe_playback import load_safe_playback_state
from .staging import load_staged_startup_config

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
STARTUP_LOAD_PREFLIGHT_KIND = "jts_active_speaker_startup_load_preflight"
STARTUP_LOAD_STATE_KIND = "jts_active_speaker_startup_load_state"
DEFAULT_STARTUP_LOAD_STATE_PATH = Path(
    "/var/lib/jasper/active_speaker_startup_load.json"
)
STARTUP_LOAD_STATE_ENV = "JASPER_ACTIVE_SPEAKER_STARTUP_LOAD_STATE"

PathLoader = Callable[[str], Awaitable[bool]]
ConfigPathReader = Callable[[], Awaitable[str | None]]


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _issue(severity: str, code: str, message: str) -> dict[str, str]:
    return {"severity": severity, "code": code, "message": message}


def _normalise_issue(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return _issue("warning", "unknown_issue", "unknown issue")
    return _issue(
        str(raw.get("severity") or "warning"),
        str(raw.get("code") or "unknown_issue"),
        str(raw.get("message") or raw.get("code") or "unknown issue"),
    )


def _gate(
    gate_id: str,
    *,
    label: str,
    passed: bool,
    message: str,
) -> dict[str, Any]:
    return {
        "id": gate_id,
        "label": label,
        "passed": bool(passed),
        "message": message,
    }


def startup_load_state_path(path: str | Path | None = None) -> Path:
    return Path(
        path or os.environ.get(STARTUP_LOAD_STATE_ENV) or DEFAULT_STARTUP_LOAD_STATE_PATH
    )


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        tmp_name = handle.name
        handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.chmod(tmp_name, 0o640)
    os.replace(tmp_name, path)


def _base_state(path: Path) -> dict[str, Any]:
    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": STARTUP_LOAD_STATE_KIND,
        "status": "idle",
        "state_path": str(path),
        "updated_at": _utc_now(),
        "loaded": False,
        "candidate_config_path": None,
        "active_config_path": None,
        "previous_config_path": None,
        "rollback_available": False,
        "last_action": "status",
        "issues": [],
    }


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
    _atomic_write_json(path, payload)


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
        "targets": staged_target_signature(staged_config)
        == topology_target_signature(topology),
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
    stop_control_available: bool = True,
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
    staged_topology = _staged_topology_payload(topology, staged)
    path_safety = _path_safety_payload(path_safety_evidence_path)
    if isinstance(path_safety.get("raw_evidence"), dict):
        path_safety_binding = validate_startup_load_evidence_binding(
            path_safety["raw_evidence"],
            topology,
            staged_config=staged,
            current_config_path=current_config_path,
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
            passed=assigned > 0 and unverified == 0,
            message=(
                "Physical output identity is verified"
                if assigned > 0 and unverified == 0
                else "Verify assigned DAC outputs before loading active DSP"
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
        _gate(
            "stop_control_available",
            label="Stop control is available",
            passed=stop_control_available,
            message=(
                "Stop is available"
                if stop_control_available else "Stop must be available"
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
        "artifact_schema_version": SCHEMA_VERSION,
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
        "artifact_schema_version": SCHEMA_VERSION,
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
    logger.info(
        "event=active_speaker.startup_rollback result=rolled_back target=%s op_id=%s",
        previous,
        apply_state.op_id,
    )
    return {"rollback": payload}
