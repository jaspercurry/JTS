# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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

from jasper.control.restart_broker import manage_units
from jasper.dsp_apply import (
    CamillaConfigValidationResult,
    DspApplyError,
    apply_dsp_config,
    validate_camilla_config,
)
from jasper.output_topology import OutputTopology, channel_identity_report

from ._common import gate as _gate, issue as _issue
from .camilla_yaml import COMMISSIONING_HEADROOM_DB, STARTUP_MUTE_GAIN_DB
from .calibration_level import (
    MIN_TEST_LEVEL_DBFS,
    load_calibration_level_state,
)
from .environment import (
    DEFAULT_CAMILLA_STATEFILE,
    classify_camilla_config_text,
    parse_camilla_statefile_config_path,
)
from .path_safety import (
    evaluate_path_safety_evidence,
    software_guard_ready_for_startup,
    staged_target_signature,
    topology_target_signature,
    validate_startup_load_evidence_binding,
)
from .safe_playback import load_safe_playback_state
from .staging import (
    SUMMED_COMMISSION_TARGET_ROLE,
    load_staged_startup_config,
    prepare_driver_commissioning_config,
    running_commission_evidence,
    staged_config_path,
)

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
STARTUP_LOAD_PREFLIGHT_KIND = "jts_active_speaker_startup_load_preflight"
STARTUP_LOAD_STATE_KIND = "jts_active_speaker_startup_load_state"
DEFAULT_STARTUP_LOAD_STATE_PATH = Path(
    "/var/lib/jasper/active_speaker_startup_load.json"
)
STARTUP_LOAD_STATE_ENV = "JASPER_ACTIVE_SPEAKER_STARTUP_LOAD_STATE"
AUDIO_HARDWARE_RECONCILE_UNIT = "jasper-audio-hardware-reconcile.service"

COMMISSION_LOAD_PREFLIGHT_KIND = "jts_active_speaker_commission_load_preflight"
COMMISSION_LOAD_STATE_KIND = "jts_active_speaker_commission_load_state"
DEFAULT_COMMISSION_LOAD_STATE_PATH = Path(
    "/var/lib/jasper/active_speaker_commission_load.json"
)
COMMISSION_LOAD_STATE_ENV = "JASPER_ACTIVE_SPEAKER_COMMISSION_LOAD_STATE"

PathLoader = Callable[[str], Awaitable[bool]]
ConfigPathReader = Callable[[], Awaitable[str | None]]
# Reads back the RUNNING CamillaDSP graph as raw YAML (active_raw over the
# websocket) — distinct from ConfigPathReader, which returns only the persisted
# config file path. The transient commissioning load applies inline configs that
# leave the persisted path unchanged, so the live-safety check needs the graph,
# not the path.
RunningConfigReader = Callable[[], Awaitable[str | None]]


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
    _trigger_audio_hardware_reconcile(source="active_speaker_startup_rollback")
    logger.info(
        "event=active_speaker.startup_rollback result=rolled_back target=%s op_id=%s",
        previous,
        apply_state.op_id,
    )
    return {"rollback": payload}


# ---------------------------------------------------------------------------
# Guarded per-driver commissioning load (gap-1 slice 2b-ii)
# ---------------------------------------------------------------------------
#
# `load_protected_startup_config` (above) loads the DURABLE all-muted staged
# boot config and persists it as the config file path CamillaDSP reboots into.
# Per-driver commissioning is different: it loads a TRANSIENT config that unmutes
# one driver, and the boot config MUST stay all-muted (crash-recovery-MUTED, see
# HANDOFF-active-speaker-dsp.md "Resolved decisions"). The two transactions share
# the same shape (snapshot → preflight gate → apply_dsp_config load with rollback)
# but differ in TWO safety-critical ways:
#
#  1. Transport. The injected `load_config` here is the INLINE loader
#     (`CamillaController.set_active_config_raw` of the file's contents), NOT the
#     path-persisting `set_config_file_path`. Inline apply changes the running
#     graph WITHOUT repointing CamillaDSP's persisted `config_file_path`, so the
#     outputd statefile keeps pointing at the all-muted staged boot config. That
#     makes crash-recovery-MUTED *structural*: a crash mid-commissioning reboots
#     into everything-muted because the statefile was never touched. (S3.)
#  2. Live confirm. Because the persisted path no longer reflects the running
#     graph, the post-load check reads the RUNNING graph back over the websocket
#     (`read_running_config` → active_raw) and asserts the mask/high-pass against
#     it with `running_commission_evidence` — the "assert the HP is present in the
#     RUNNING pipeline, not just the file" gate. A failed live check rolls back to
#     the staged anchor inside the apply transaction.
#
# Evidence is re-derived at load time by re-running
# `prepare_driver_commissioning_config` (S2): the load never trusts a persisted or
# browser-supplied verdict. Scope: the audible mask is the target's whole role on
# the single active speaker group (mono jts3 = one output); per-SIDE isolation is
# a future selector (S1, see prepare's docstring).


def commission_load_state_path(path: str | Path | None = None) -> Path:
    return Path(
        path
        or os.environ.get(COMMISSION_LOAD_STATE_ENV)
        or DEFAULT_COMMISSION_LOAD_STATE_PATH
    )


def _commission_base_state(path: Path) -> dict[str, Any]:
    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": COMMISSION_LOAD_STATE_KIND,
        "status": "idle",
        "state_path": str(path),
        "updated_at": _utc_now(),
        "loaded": False,
        "candidate_config_path": None,
        "active_config_path": None,
        "previous_config_path": None,
        "rollback_available": False,
        "last_action": "status",
        "target": {},
        "runtime_status": {},
        "issues": [],
    }


def load_commission_load_state(
    *,
    state_path: str | Path | None = None,
) -> dict[str, Any]:
    """Return the latest per-driver commissioning load/rollback state."""

    path = commission_load_state_path(state_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _commission_base_state(path)
    if not isinstance(payload, dict):
        return _commission_base_state(path)
    state = _commission_base_state(path)
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


def _record_commission_state(
    payload: dict[str, Any],
    *,
    state_path: str | Path | None = None,
) -> None:
    path = commission_load_state_path(state_path)
    payload = dict(payload)
    payload["state_path"] = str(path)
    payload["updated_at"] = payload.get("updated_at") or _utc_now()
    _atomic_write_json(path, payload)


def _commission_state_payload(
    *,
    status: str,
    candidate_config_path: str | None,
    active_config_path: str | None,
    previous_config_path: str | None,
    last_action: str,
    target: dict[str, Any] | None = None,
    audible_evidence: dict[str, Any] | None = None,
    live_evidence: dict[str, Any] | None = None,
    durable_statefile_target: str | None = None,
    durable_statefile_intact: bool | None = None,
    preflight: dict[str, Any] | None = None,
    dsp_apply: dict[str, Any] | None = None,
    issues: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    loaded = status == "loaded"
    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": COMMISSION_LOAD_STATE_KIND,
        "status": status,
        "updated_at": _utc_now(),
        "loaded": loaded,
        "candidate_config_path": candidate_config_path,
        "active_config_path": active_config_path,
        "previous_config_path": previous_config_path,
        "rollback_available": bool(loaded and previous_config_path),
        "last_action": last_action,
        "target": target or {},
        "audible_evidence": _compact_evidence(audible_evidence),
        "live_evidence": _compact_evidence(live_evidence),
        "durable_statefile_target": durable_statefile_target,
        "durable_statefile_intact": durable_statefile_intact,
        "preflight_status": (preflight or {}).get("status"),
        "dsp_apply": dsp_apply,
        "issues": issues or [],
    }


def _compact_evidence(evidence: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(evidence, dict):
        return {}
    payload = {
        "passed": bool(evidence.get("passed")),
        "checks": dict(evidence.get("checks") or {}),
    }
    for key in (
        "audible_outputs",
        "muted_outputs",
        "tweeter_outputs",
        "audible_tweeter_outputs",
        "protective_highpass_hz",
    ):
        if key in evidence:
            payload[key] = evidence.get(key)
    return payload


def _commission_live_mask(state: dict[str, Any]) -> dict[str, Any]:
    """Return the saved mask needed to re-prove a transient commission load.

    Newer state files persist the compact mask from the original off-device
    evidence. Older files only have the target output; those are deliberately
    treated as stale because they cannot prove every non-target output is muted.
    """

    evidence = state.get("audible_evidence")
    if not isinstance(evidence, dict):
        evidence = {}
    live_evidence = state.get("live_evidence")
    if not isinstance(live_evidence, dict):
        live_evidence = {}
    target = state.get("target")
    if not isinstance(target, dict):
        target = {}

    audible = evidence.get("audible_outputs") or live_evidence.get("audible_outputs")
    muted = evidence.get("muted_outputs") or live_evidence.get("muted_outputs")
    tweeters = evidence.get("tweeter_outputs") or live_evidence.get("tweeter_outputs")
    hp_hz = (
        evidence.get("protective_highpass_hz")
        if "protective_highpass_hz" in evidence
        else live_evidence.get("protective_highpass_hz")
    )
    if audible is None:
        audible = target.get("audible_outputs")
    return {
        "audible_outputs": audible if isinstance(audible, list) else [],
        "muted_outputs": muted if isinstance(muted, list) else [],
        "tweeter_outputs": tweeters if isinstance(tweeters, list) else [],
        "protective_highpass_hz": hp_hz,
        "complete": isinstance(audible, list)
        and isinstance(muted, list)
        and isinstance(tweeters, list),
    }


def commission_load_runtime_status(
    state: dict[str, Any],
    running_config_raw: str | None,
) -> dict[str, Any]:
    """Compare persisted commission-load state to the live Camilla graph.

    Per-driver commissioning is intentionally transient: it is uploaded with
    CamillaDSP's inline ``SetConfig`` path and the durable statefile remains
    pointed at the all-muted startup graph. Therefore the JSON state is only
    true while the live graph still proves the saved mask. A service restart,
    Camilla restart, or returned browser session can leave the JSON behind; this
    helper fails closed and reports that as ``stale``.
    """

    status = str(state.get("status") or "idle")
    if status != "loaded":
        return {
            "kind": "jts_active_speaker_commission_load_runtime_status",
            "status": status,
            "loaded": False,
            "stale": False,
            "checks": {"persisted_loaded": False},
            "issues": [],
        }

    mask = _commission_live_mask(state)
    checks = {
        "persisted_loaded": True,
        "mask_evidence_complete": bool(mask["complete"]),
    }
    live: dict[str, Any] = {}
    if mask["complete"]:
        live = running_commission_evidence(
            running_config_raw,
            audible_outputs=mask["audible_outputs"],
            muted_outputs=mask["muted_outputs"],
            tweeter_outputs=mask["tweeter_outputs"],
            protective_hp_hz=mask["protective_highpass_hz"],
            expected_headroom_db=COMMISSIONING_HEADROOM_DB,
        )
        checks["live_mask_and_highpass"] = bool(live.get("passed"))
    else:
        checks["live_mask_and_highpass"] = False
    loaded = all(checks.values())
    issue = None if loaded else _issue(
        "blocker",
        "commission_live_state_stale",
        (
            "the saved driver test session no longer matches the live "
            "CamillaDSP graph; start the tone again to re-open it"
        ),
    )
    return {
        "kind": "jts_active_speaker_commission_load_runtime_status",
        "status": "loaded" if loaded else "stale",
        "loaded": loaded,
        "stale": not loaded,
        "checks": checks,
        "live_evidence": _compact_evidence(live),
        "target": state.get("target") or {},
        "issues": [issue] if issue else [],
    }


def commission_load_state_with_runtime_status(
    state: dict[str, Any],
    runtime_status: dict[str, Any],
) -> dict[str, Any]:
    """Overlay read-only live status onto persisted commission-load state."""

    out = dict(state)
    out["runtime_status"] = runtime_status
    if state.get("status") == "loaded" and runtime_status.get("status") == "stale":
        out["status"] = "stale"
        out["loaded"] = False
        out["rollback_available"] = False
        out["issues"] = [
            *[
                issue
                for issue in out.get("issues", [])
                if isinstance(issue, dict)
            ],
            *runtime_status.get("issues", []),
        ]
    return out


def mark_commission_load_state_stale(
    state: dict[str, Any],
    runtime_status: dict[str, Any],
    *,
    state_path: str | Path | None = None,
) -> dict[str, Any]:
    """Persist that a previously loaded transient commission graph has expired."""

    payload = commission_load_state_with_runtime_status(state, runtime_status)
    payload["last_action"] = "stale_detected"
    payload["runtime_status"] = runtime_status
    _record_commission_state(payload, state_path=state_path)
    return load_commission_load_state(state_path=state_path)


def _read_statefile_config_path(statefile_path: str | Path | None) -> str | None:
    """Return the config_path the outputd/CamillaDSP statefile boots into."""

    path = (
        Path(statefile_path)
        if statefile_path is not None
        else Path(
            os.environ.get("JASPER_CAMILLA_STATEFILE", str(DEFAULT_CAMILLA_STATEFILE))
        )
    )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return parse_camilla_statefile_config_path(text)


def _paths_equal(a: str | Path | None, b: str | Path | None) -> bool:
    """Compare two config paths, dereferencing symlinks where possible.

    Config paths reach us from heterogeneous sources — a staged metadata dict, a
    `staged_config_path()` default, and a CamillaDSP statefile's raw YAML — that
    may spell the same file differently (symlink vs real path). Resolve both so a
    safety equality check (active-graph-is-staged, statefile-still-staged) does
    not spuriously fail on a benign spelling difference.
    """

    if not a or not b:
        return False
    try:
        return Path(str(a)).resolve() == Path(str(b)).resolve()
    except (OSError, RuntimeError):
        return Path(str(a)) == Path(str(b))


def build_driver_commission_load_preflight(
    topology: OutputTopology,
    *,
    speaker_group_id: str,
    role: str,
    calibration_level: dict[str, Any] | None = None,
    staged_config: dict[str, Any] | None = None,
    preset: Any = None,
    crossover_preview: dict[str, Any] | None = None,
    playback_device: str | None = None,
    audible_gain_db: float = STARTUP_MUTE_GAIN_DB,
    path_safety_evidence_path: str | Path | None = None,
    current_config_path: str | Path | None = None,
    config_dir: str | Path | None = None,
    config_path: str | Path | None = None,
    validate: Callable[[str | Path], CamillaConfigValidationResult] = (
        validate_camilla_config
    ),
) -> dict[str, Any]:
    """Deterministic preflight for the guarded per-driver commissioning load.

    Two independent proofs must both hold: (a) the speaker is ready to load an
    active config and the all-muted staged config is a valid rollback anchor —
    reuses :func:`build_startup_load_preflight` (path-safety, calibration floor,
    physical identity, no active tone playback); (b) the per-driver candidate is
    safe — re-runs :func:`prepare_driver_commissioning_config` so the evidence is
    fresh, never persisted or browser-supplied (S2).
    """

    staged = (
        staged_config
        if isinstance(staged_config, dict)
        else load_staged_startup_config()
    )
    startup = build_startup_load_preflight(
        topology,
        staged_config=staged,
        calibration_level=calibration_level,
        path_safety_evidence_path=path_safety_evidence_path,
        current_config_path=current_config_path,
        validate=validate,
    )
    prepare = prepare_driver_commissioning_config(
        topology,
        speaker_group_id=speaker_group_id,
        role=role,
        preset=preset,
        crossover_preview=crossover_preview,
        playback_device=playback_device,
        audible_gain_db=audible_gain_db,
        config_dir=config_dir,
        config_path=config_path,
        validate=validate,
    )

    speaker_ready = bool(startup.get("load_allowed"))
    prepared = prepare.get("status") == "prepared"
    audible_passed = bool((prepare.get("audible_evidence") or {}).get("passed"))
    candidate = prepare.get("config") or {}
    candidate_present = bool(candidate.get("exists"))

    gates = [
        _gate(
            "speaker_ready_for_active_load",
            label="Speaker and all-muted rollback anchor are ready for an active load",
            passed=speaker_ready,
            message=(
                "Speaker, path-safety, and staged rollback anchor are ready"
                if speaker_ready
                else "Resolve protected startup-load blockers before commissioning"
            ),
        ),
        _gate(
            "commissioning_candidate_prepared",
            label="Per-driver commissioning config is prepared",
            passed=prepared,
            message=(
                "Per-driver commissioning config is prepared"
                if prepared
                else "Resolve per-driver commissioning preparation blockers"
            ),
        ),
        _gate(
            "commissioning_protection_while_audible",
            label="Per-driver protection-while-audible evidence passed",
            passed=audible_passed,
            message=(
                "Only the target is audible and its protection is intact"
                if audible_passed
                else "Per-driver protection-while-audible evidence did not pass"
            ),
        ),
        _gate(
            "commissioning_candidate_present",
            label="Generated commissioning config exists on disk",
            passed=candidate_present,
            message=(
                "Generated commissioning config is on disk"
                if candidate_present
                else "Generated commissioning config is missing"
            ),
        ),
    ]
    issues: list[dict[str, str]] = []
    issues.extend(
        _normalise_issue(issue)
        for issue in startup.get("issues", [])
        if isinstance(issue, dict)
    )
    issues.extend(
        _normalise_issue(issue)
        for issue in prepare.get("issues", [])
        if isinstance(issue, dict)
    )
    load_allowed = (
        speaker_ready
        and prepared
        and audible_passed
        and candidate_present
        and all(gate["passed"] for gate in gates)
    )
    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": COMMISSION_LOAD_PREFLIGHT_KIND,
        "status": "ready" if load_allowed else "blocked",
        "load_allowed": load_allowed,
        "target": prepare.get("target") or {},
        "candidate_config_path": candidate.get("path"),
        "audible_evidence": prepare.get("audible_evidence") or {},
        "startup_preflight": startup,
        "prepare": prepare,
        "required_gates": gates,
        "issues": issues,
        "next_step": (
            "Ready to load the per-driver commissioning config into the running "
            "CamillaDSP graph. The durable boot config stays all-muted."
            if load_allowed
            else "Resolve per-driver commissioning load blockers before reloading."
        ),
    }


async def load_driver_commissioning_config(
    topology: OutputTopology,
    *,
    speaker_group_id: str,
    role: str,
    load_config: PathLoader,
    read_running_config: RunningConfigReader,
    get_current_config_path: ConfigPathReader,
    calibration_level: dict[str, Any] | None = None,
    preset: Any = None,
    crossover_preview: dict[str, Any] | None = None,
    playback_device: str | None = None,
    audible_gain_db: float = STARTUP_MUTE_GAIN_DB,
    path_safety_evidence_path: str | Path | None = None,
    staged_config: dict[str, Any] | None = None,
    config_dir: str | Path | None = None,
    config_path: str | Path | None = None,
    statefile_path: str | Path | None = None,
    state_path: str | Path | None = None,
    reconcile_output_hardware: bool = True,
    validate: Callable[[str | Path], CamillaConfigValidationResult] = (
        validate_camilla_config
    ),
) -> dict[str, Any]:
    """Load a per-driver commissioning config into the RUNNING CamillaDSP graph.

    Transient and rollback-able: applies the prepared config with the INLINE
    loader (``load_config`` = ``set_active_config_raw`` of the file's contents) so
    the durable boot config / outputd statefile stay pointed at the all-muted
    staged config (crash-recovery-MUTED). After the apply, the RUNNING graph is
    read back (``read_running_config``) and re-asserted with
    :func:`running_commission_evidence`; a failed check rolls back to the staged
    anchor. ``get_current_config_path`` reads the persisted statefile path (for
    the path-safety binding + the durable-statefile S3 fact), NOT the running
    graph.

    Precondition: the active graph (the staged all-muted config) is already live
    via :func:`load_protected_startup_config`. Commissioning swaps the running
    graph at the same width. The first arm waits for the output-hardware
    reconciler so outputd is reading the active lane before the audible ramp can
    start; later same-target ramp updates may skip that reconciler because they
    only change the transient CamillaDSP gain/mask, not the output lane.
    """

    try:
        prior_config_path = await get_current_config_path()
    except Exception as exc:  # noqa: BLE001
        issue = _issue(
            "blocker",
            "current_config_snapshot_failed",
            f"could not read current CamillaDSP config path: {type(exc).__name__}",
        )
        payload = _commission_state_payload(
            status="failed",
            candidate_config_path=None,
            active_config_path=None,
            previous_config_path=None,
            last_action="load_failed",
            issues=[issue],
        )
        _record_commission_state(payload, state_path=state_path)
        return {"preflight": None, "load": payload}

    staged = (
        staged_config
        if isinstance(staged_config, dict)
        else load_staged_startup_config()
    )
    preflight = build_driver_commission_load_preflight(
        topology,
        speaker_group_id=speaker_group_id,
        role=role,
        calibration_level=calibration_level,
        staged_config=staged,
        preset=preset,
        crossover_preview=crossover_preview,
        playback_device=playback_device,
        audible_gain_db=audible_gain_db,
        path_safety_evidence_path=path_safety_evidence_path,
        current_config_path=prior_config_path,
        config_dir=config_dir,
        config_path=config_path,
        validate=validate,
    )
    target = preflight.get("target") or {}
    candidate_path = preflight.get("candidate_config_path")
    evidence = preflight.get("audible_evidence") or {}
    staged_path = (staged.get("config") or {}).get("path") or str(staged_config_path())

    if not preflight.get("load_allowed"):
        payload = _commission_state_payload(
            status="blocked",
            candidate_config_path=candidate_path,
            active_config_path=None,
            previous_config_path=staged_path,
            last_action="load_blocked",
            target=target,
            audible_evidence=evidence,
            preflight=preflight,
            issues=[
                _normalise_issue(issue)
                for issue in preflight.get("issues", [])
                if isinstance(issue, dict)
            ],
        )
        _record_commission_state(payload, state_path=state_path)
        logger.info(
            "event=active_speaker.driver_commission_load result=blocked group=%s role=%s blockers=%d",
            speaker_group_id,
            role,
            len(payload["issues"]),
        )
        return {"preflight": preflight, "load": payload}

    if not Path(str(staged_path)).exists():
        issue = _issue(
            "blocker",
            "commission_rollback_anchor_missing",
            f"all-muted staged rollback anchor does not exist: {staged_path}",
        )
        payload = _commission_state_payload(
            status="blocked",
            candidate_config_path=candidate_path,
            active_config_path=None,
            previous_config_path=str(staged_path),
            last_action="load_blocked",
            target=target,
            audible_evidence=evidence,
            preflight=preflight,
            issues=[issue],
        )
        _record_commission_state(payload, state_path=state_path)
        logger.info(
            "event=active_speaker.driver_commission_load result=blocked reason=rollback_anchor_missing anchor=%s",
            staged_path,
        )
        return {"preflight": preflight, "load": payload}

    # Precondition gate: the persisted (boot / rollback-anchor) config must
    # ALREADY be the all-muted staged config (loaded by load_protected_startup_config
    # at Stage 4 via set_config_file_path). Commissioning swaps the running graph
    # at the same width and rolls back to this anchor; running it when the boot
    # config is an unrelated graph (a stereo/correction config) is not a safe
    # transition. Fail closed rather than swap blindly.
    if not _paths_equal(prior_config_path, staged_path):
        issue = _issue(
            "blocker",
            "commission_active_graph_not_staged",
            (
                "per-driver commissioning requires the all-muted staged config to be "
                f"the persisted boot config first; current persisted config is "
                f"{prior_config_path or '(none)'}"
            ),
        )
        payload = _commission_state_payload(
            status="blocked",
            candidate_config_path=candidate_path,
            active_config_path=None,
            previous_config_path=str(staged_path),
            last_action="load_blocked",
            target=target,
            audible_evidence=evidence,
            preflight=preflight,
            issues=[issue],
        )
        _record_commission_state(payload, state_path=state_path)
        logger.info(
            "event=active_speaker.driver_commission_load result=blocked reason=active_graph_not_staged current=%s anchor=%s",
            prior_config_path,
            staged_path,
        )
        return {"preflight": preflight, "load": payload}

    captured: dict[str, Any] = {"evidence": evidence}

    def _emit_in_lock() -> None:
        # apply_dsp_config runs this inside its writer lock, immediately before
        # validate+load. Re-emitting the candidate here (rather than trusting the
        # file the preflight wrote) closes the write→read window on the shared
        # commissioning path: no concurrent prepare can overwrite the candidate
        # between gate and load, and the live-confirm mask is re-derived fresh.
        # `run_config_check=False` — apply_dsp_config's own validate(candidate)
        # is the load-time syntax gate; the preflight already ran the full check.
        fresh = prepare_driver_commissioning_config(
            topology,
            speaker_group_id=speaker_group_id,
            role=role,
            preset=preset,
            crossover_preview=crossover_preview,
            playback_device=playback_device,
            audible_gain_db=audible_gain_db,
            config_dir=config_dir,
            config_path=config_path,
            run_config_check=False,
            validate=validate,
        )
        if fresh.get("status") != "prepared" or not (
            fresh.get("audible_evidence") or {}
        ).get("passed"):
            raise RuntimeError(
                "per-driver commissioning config no longer prepares cleanly under lock"
            )
        captured["evidence"] = fresh.get("audible_evidence") or {}

    async def _live_confirm() -> None:
        # Runs inside apply_dsp_config's lock, after the inline load. Raising here
        # rolls the running graph back to the staged anchor INSIDE the lock, so
        # both safety checks below fail closed atomically.
        live_evidence = captured["evidence"]
        # (1) The RUNNING graph (read back over the websocket, not the file) must
        #     match the intended per-driver mask + keep the protective high-pass.
        try:
            running_raw = await read_running_config()
        except Exception as exc:  # noqa: BLE001
            captured["live"] = {
                "passed": False,
                "checks": {"running_config_readable": False},
            }
            raise RuntimeError(
                f"could not read back the running CamillaDSP graph: {type(exc).__name__}"
            ) from exc
        live = running_commission_evidence(
            running_raw,
            audible_outputs=live_evidence.get("audible_outputs", []),
            muted_outputs=live_evidence.get("muted_outputs", []),
            tweeter_outputs=live_evidence.get("tweeter_outputs", []),
            protective_hp_hz=live_evidence.get("protective_highpass_hz"),
            expected_headroom_db=COMMISSIONING_HEADROOM_DB,
        )
        captured["live"] = live
        if not live["passed"]:
            failed = sorted(
                code for code, ok in live.get("checks", {}).items() if not ok
            )
            raise RuntimeError(
                "running CamillaDSP graph failed live commissioning safety: "
                + ", ".join(failed)
            )
        # (2) S3: the durable boot config / outputd statefile MUST still point at
        #     the all-muted staged config — the transient graph is in RUNNING
        #     CamillaDSP only. Structurally guaranteed by the inline transport
        #     (set_active_config_raw never repoints the persisted path); a drift
        #     here means something else moved it, so fail closed (reboot would
        #     otherwise come up on the transient config — crash-recovery-MUTED).
        durable_target = _read_statefile_config_path(statefile_path)
        captured["durable_target"] = durable_target
        captured["durable_intact"] = (
            None if durable_target is None else _paths_equal(durable_target, staged_path)
        )
        if durable_target is not None and not captured["durable_intact"]:
            raise RuntimeError(
                f"durable CamillaDSP statefile drifted to {durable_target}, expected "
                f"the all-muted staged boot config {staged_path}"
            )

    try:
        apply_state = await apply_dsp_config(
            source="active_speaker_driver_commission_load",
            candidate_path=str(candidate_path),
            prior_config_path=str(staged_path),
            load_config=load_config,
            # Inline transport: the persisted path stays the staged anchor, so the
            # running graph never equals the candidate path. Skip apply's
            # path-confirm; the live read-back below is the real confirm.
            get_current_config_path=None,
            prepare=_emit_in_lock,
            persist=_live_confirm,
            validate=validate,
        )
    except DspApplyError as exc:
        payload = _commission_state_payload(
            status="failed",
            candidate_config_path=candidate_path,
            active_config_path=None,
            previous_config_path=str(staged_path),
            last_action="load_failed",
            target=target,
            audible_evidence=evidence,
            live_evidence=captured.get("live"),
            durable_statefile_target=captured.get("durable_target"),
            durable_statefile_intact=captured.get("durable_intact"),
            preflight=preflight,
            dsp_apply=exc.state.to_dict(),
            issues=[
                _issue(
                    "blocker",
                    "driver_commission_load_failed",
                    f"CamillaDSP commissioning load failed (rolled back to staged): {exc}",
                )
            ],
        )
        _record_commission_state(payload, state_path=state_path)
        # Surface the safety reason (live-mask drift / missing HP / statefile
        # drift / unreadable graph) in the journal, not just the state file — the
        # journal is the operator's first debug surface.
        reason = exc.state.persist_error or exc.state.load_error or str(exc)
        logger.warning(
            "event=active_speaker.driver_commission_load result=failed candidate=%s anchor=%s "
            "rolled_back=%s reason=%s",
            candidate_path,
            staged_path,
            getattr(exc.state, "rollback_succeeded", None),
            reason,
        )
        return {"preflight": preflight, "load": payload}

    # live-confirm + S3 both passed inside the lock.
    payload = _commission_state_payload(
        status="loaded",
        candidate_config_path=str(candidate_path),
        active_config_path=apply_state.active_config_path or str(candidate_path),
        previous_config_path=str(staged_path),
        last_action="load",
        target=target,
        audible_evidence=evidence,
        live_evidence=captured.get("live"),
        durable_statefile_target=captured.get("durable_target"),
        durable_statefile_intact=captured.get("durable_intact"),
        preflight=preflight,
        dsp_apply=apply_state.to_dict(),
        issues=[],
    )
    if not reconcile_output_hardware:
        payload["output_reconcile"] = {
            "status": "skipped",
            "reason": "same_active_output_lane",
            "unit": AUDIO_HARDWARE_RECONCILE_UNIT,
        }
        _record_commission_state(payload, state_path=state_path)
        logger.info(
            "event=active_speaker.driver_commission_load action=output_reconcile "
            "status=skipped reason=same_active_output_lane"
        )
        logger.info(
            "event=active_speaker.driver_commission_load result=loaded candidate=%s anchor=%s "
            "durable_intact=%s op_id=%s",
            candidate_path,
            staged_path,
            captured.get("durable_intact"),
            apply_state.op_id,
        )
        return {"preflight": preflight, "load": payload}

    if not _trigger_audio_hardware_reconcile(
        source="active_speaker_driver_commission_load"
    ):
        payload = _commission_state_payload(
            status="failed",
            candidate_config_path=str(candidate_path),
            active_config_path=apply_state.active_config_path or str(candidate_path),
            previous_config_path=str(staged_path),
            last_action="output_reconcile_failed",
            target=target,
            audible_evidence=evidence,
            live_evidence=captured.get("live"),
            durable_statefile_target=captured.get("durable_target"),
            durable_statefile_intact=captured.get("durable_intact"),
            preflight=preflight,
            dsp_apply=apply_state.to_dict(),
            issues=[
                _issue(
                    "blocker",
                    "commission_output_hardware_reconcile_failed",
                    "could not switch outputd to the active driver lane before tone playback",
                )
            ],
        )
        payload["output_reconcile"] = {
            "status": "failed",
            "unit": AUDIO_HARDWARE_RECONCILE_UNIT,
        }
        _record_commission_state(payload, state_path=state_path)
        logger.warning(
            "event=active_speaker.driver_commission_load result=failed candidate=%s anchor=%s "
            "reason=output_hardware_reconcile_failed op_id=%s",
            candidate_path,
            staged_path,
            apply_state.op_id,
        )
        return {"preflight": preflight, "load": payload}
    payload["output_reconcile"] = {
        "status": "succeeded",
        "unit": AUDIO_HARDWARE_RECONCILE_UNIT,
    }
    _record_commission_state(payload, state_path=state_path)
    logger.info(
        "event=active_speaker.driver_commission_load result=loaded candidate=%s anchor=%s "
        "durable_intact=%s op_id=%s",
        candidate_path,
        staged_path,
        captured.get("durable_intact"),
        apply_state.op_id,
    )
    return {"preflight": preflight, "load": payload}


async def load_summed_commissioning_config(
    topology: OutputTopology,
    *,
    speaker_group_id: str,
    load_config: PathLoader,
    read_running_config: RunningConfigReader,
    get_current_config_path: ConfigPathReader,
    calibration_level: dict[str, Any] | None = None,
    preset: Any = None,
    crossover_preview: dict[str, Any] | None = None,
    playback_device: str | None = None,
    audible_gain_db: float = STARTUP_MUTE_GAIN_DB,
    path_safety_evidence_path: str | Path | None = None,
    staged_config: dict[str, Any] | None = None,
    config_dir: str | Path | None = None,
    config_path: str | Path | None = None,
    statefile_path: str | Path | None = None,
    state_path: str | Path | None = None,
    reconcile_output_hardware: bool = True,
    validate: Callable[[str | Path], CamillaConfigValidationResult] = (
        validate_camilla_config
    ),
) -> dict[str, Any]:
    """Load the combined-driver commissioning graph into RUNNING CamillaDSP.

    Public semantic wrapper for the validation-card summed check. It reuses the
    same masked commissioning runtime as per-driver bring-up so rollback,
    durable all-muted boot anchoring, live graph read-back, and hardware
    reconcile behavior stay identical.
    """

    payload = await load_driver_commissioning_config(
        topology,
        speaker_group_id=speaker_group_id,
        role=SUMMED_COMMISSION_TARGET_ROLE,
        calibration_level=calibration_level,
        load_config=load_config,
        read_running_config=read_running_config,
        get_current_config_path=get_current_config_path,
        preset=preset,
        crossover_preview=crossover_preview,
        playback_device=playback_device,
        audible_gain_db=audible_gain_db,
        path_safety_evidence_path=path_safety_evidence_path,
        staged_config=staged_config,
        config_dir=config_dir,
        config_path=config_path,
        statefile_path=statefile_path,
        state_path=state_path,
        reconcile_output_hardware=reconcile_output_hardware,
        validate=validate,
    )
    payload["operation"] = "summed_commissioning"
    return payload


async def rollback_driver_commissioning_config(
    *,
    load_config: PathLoader,
    state_path: str | Path | None = None,
    validate: Callable[[str | Path], CamillaConfigValidationResult] = (
        validate_camilla_config
    ),
) -> dict[str, Any]:
    """Reload the all-muted staged config, ending a per-driver commissioning load.

    Re-applies the durable boot config into the RUNNING graph (inline, same as
    the load), returning the speaker to everything-muted. The statefile already
    points at the staged config, so nothing durable changes — this only un-does
    the transient runtime swap.
    """

    current_state = load_commission_load_state(state_path=state_path)
    previous = current_state.get("previous_config_path")
    if current_state.get("status") != "loaded" or not previous:
        issue = _issue(
            "blocker",
            "commission_rollback_unavailable",
            "no loaded per-driver commissioning config has a rollback target",
        )
        payload = _commission_state_payload(
            status="blocked",
            candidate_config_path=current_state.get("candidate_config_path"),
            active_config_path=current_state.get("active_config_path"),
            previous_config_path=previous,
            last_action="rollback_blocked",
            target=current_state.get("target"),
            issues=[issue],
        )
        return {"rollback": payload}
    if not Path(str(previous)).exists():
        issue = _issue(
            "blocker",
            "commission_rollback_config_missing",
            f"rollback config no longer exists: {previous}",
        )
        payload = _commission_state_payload(
            status="rollback_failed",
            candidate_config_path=current_state.get("candidate_config_path"),
            active_config_path=current_state.get("active_config_path"),
            previous_config_path=str(previous),
            last_action="rollback_failed",
            target=current_state.get("target"),
            issues=[issue],
        )
        _record_commission_state(payload, state_path=state_path)
        return {"rollback": payload}

    try:
        apply_state = await apply_dsp_config(
            source="active_speaker_driver_commission_rollback",
            candidate_path=str(previous),
            prior_config_path=None,
            get_current_config_path=None,
            load_config=load_config,
            validate=validate,
        )
    except Exception as exc:  # noqa: BLE001
        dsp_state = exc.state.to_dict() if isinstance(exc, DspApplyError) else None
        payload = _commission_state_payload(
            status="rollback_failed",
            candidate_config_path=current_state.get("candidate_config_path"),
            active_config_path=current_state.get("active_config_path"),
            previous_config_path=str(previous),
            last_action="rollback_failed",
            target=current_state.get("target"),
            dsp_apply=dsp_state,
            issues=[
                _issue(
                    "blocker",
                    "commission_rollback_failed",
                    f"CamillaDSP commissioning rollback failed: {exc}",
                )
            ],
        )
        _record_commission_state(payload, state_path=state_path)
        logger.warning(
            "event=active_speaker.driver_commission_rollback result=failed target=%s error=%s",
            previous,
            type(exc).__name__,
        )
        return {"rollback": payload}

    payload = _commission_state_payload(
        status="rolled_back",
        candidate_config_path=current_state.get("candidate_config_path"),
        active_config_path=apply_state.active_config_path or str(previous),
        previous_config_path=str(previous),
        last_action="rollback",
        target=current_state.get("target"),
        dsp_apply=apply_state.to_dict(),
    )
    _record_commission_state(payload, state_path=state_path)
    logger.info(
        "event=active_speaker.driver_commission_rollback result=rolled_back target=%s op_id=%s",
        previous,
        apply_state.op_id,
    )
    return {"rollback": payload}
