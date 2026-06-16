"""Deterministic safety gates for active-speaker audible paths.

Before JTS can load an active crossover onto real hardware, every
audible source path must be proven to enter the protected active
baseline. This module encodes that checklist as data and evaluates
operator or hardware-probe evidence. The first hardware-probe slice is
still no-audio: it inspects the saved output topology, staged protected
startup candidate, calibration-level guard, and current CamillaDSP config
path, then writes evidence for the startup-load gate.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jasper.output_topology import OutputTopology, channel_identity_report

from ._common import issue as _issue
from .profile import ActiveSpeakerConfigError

SCHEMA_VERSION = 1
PATH_SAFETY_EVIDENCE_KIND = "jts_active_speaker_path_safety_evidence"
PATH_SAFETY_EVIDENCE_ENV = "JASPER_ACTIVE_SPEAKER_PATH_SAFETY_EVIDENCE"
DEFAULT_PATH_SAFETY_EVIDENCE_PATH = Path(
    "/var/lib/jasper/active_speaker_path_safety.json"
)
OPERATOR_EVIDENCE_SOURCE = "operator"
HARDWARE_PROBE_EVIDENCE_SOURCE = "hardware_probe"
SUPPORTED_EVIDENCE_SOURCES = {
    OPERATOR_EVIDENCE_SOURCE,
    HARDWARE_PROBE_EVIDENCE_SOURCE,
}


@dataclass(frozen=True)
class PathSafetyRequirement:
    id: str
    label: str
    checks: tuple[str, ...]
    why: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "checks": list(self.checks),
            "why": self.why,
        }


REQUIRED_PATHS: tuple[PathSafetyRequirement, ...] = (
    PathSafetyRequirement(
        id="music_renderers",
        label="Music renderers",
        checks=("route_verified", "protected_by_active_baseline", "bypass_disabled"),
        why=(
            "Spotify, AirPlay, Bluetooth, USB input, and local renderers "
            "must not reach raw active outputs."
        ),
    ),
    PathSafetyRequirement(
        id="tts_cues",
        label="TTS and cues",
        checks=("route_verified", "protected_by_active_baseline", "bypass_disabled"),
        why=(
            "Assistant speech and cue WAVs are audible paths and can damage "
            "a tweeter if they bypass the crossover."
        ),
    ),
    PathSafetyRequirement(
        id="correction_sweeps",
        label="Correction sweeps",
        checks=(
            "route_verified",
            "protected_by_active_baseline",
            "level_controlled",
            "bypass_disabled",
        ),
        why=(
            "Room-correction sweeps are intentional wideband playback and "
            "must run through protection."
        ),
    ),
    PathSafetyRequirement(
        id="test_tones",
        label="Test tones",
        checks=("route_verified", "protected_by_active_baseline", "level_controlled"),
        why=(
            "Commissioning tones must be quiet, band-limited where "
            "appropriate, and routed through the baseline."
        ),
    ),
    PathSafetyRequirement(
        id="rollback_configs",
        label="Rollback configs",
        checks=(
            "route_verified",
            "protected_by_active_baseline",
            "rollback_target_available",
            "rollback_target_restore_limited",
        ),
        why=(
            "Loading a protected startup graph must keep a readable, "
            "bounded previous config to restore if the graph transition fails."
        ),
    ),
    PathSafetyRequirement(
        id="startup_reload",
        label="Startup and reload state",
        checks=(
            "active_outputs_muted",
            "protected_by_active_baseline",
            "no_raw_fullrange",
        ),
        why=(
            "Daemon start, config reload, and crash recovery must not "
            "produce unprotected output."
        ),
    ),
)


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


def path_safety_evidence_path(path: str | Path | None = None) -> Path:
    """Return the configured path-safety evidence artifact path."""

    return Path(
        path
        or os.environ.get(PATH_SAFETY_EVIDENCE_ENV)
        or DEFAULT_PATH_SAFETY_EVIDENCE_PATH
    )


def requirements_payload() -> dict[str, Any]:
    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": "jts_active_speaker_path_safety_requirements",
        "requirements": [path.to_dict() for path in REQUIRED_PATHS],
    }


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


def write_path_safety_evidence(
    evidence: dict[str, Any],
    *,
    path: str | Path | None = None,
) -> Path:
    """Persist a path-safety evidence artifact atomically."""

    target = path_safety_evidence_path(path)
    _atomic_write_json(target, evidence)
    return target


def topology_target_signature(topology: OutputTopology) -> list[dict[str, Any]]:
    """Return the path-safety-relevant target identity for a topology."""

    targets: list[dict[str, Any]] = []
    for group in topology.speaker_groups:
        for channel in group.channels:
            targets.append({
                "speaker_group_id": group.id,
                "role": channel.role,
                "physical_output_index": channel.physical_output_index,
                "identity_verified": bool(channel.identity_verified),
                "startup_muted": bool(channel.startup_muted),
                "protection_required": bool(channel.protection_required),
                "protection_status": channel.protection_status,
            })
    return sorted(targets, key=lambda item: (item["speaker_group_id"], item["role"]))


def staged_target_signature(staged_config: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the staged startup config's path-safety-relevant target identity."""

    targets = staged_config.get("targets")
    if not isinstance(targets, list):
        return []
    out: list[dict[str, Any]] = []
    for raw in targets:
        if not isinstance(raw, dict):
            continue
        out.append({
            "speaker_group_id": raw.get("speaker_group_id"),
            "role": raw.get("role"),
            "physical_output_index": raw.get("physical_output_index"),
            "identity_verified": bool(raw.get("identity_verified")),
            "startup_muted": bool(raw.get("startup_muted")),
            "protection_required": bool(raw.get("protection_required")),
            "protection_status": raw.get("protection_status"),
        })
    return sorted(
        out,
        key=lambda item: (str(item["speaker_group_id"]), str(item["role"])),
    )


def software_guard_ready_for_startup(
    topology: OutputTopology,
    staged_config: dict[str, Any],
) -> bool:
    """Return whether software-only compression-driver guard evidence is ready."""

    software_guard_requested = any(
        channel.role == "tweeter"
        and channel.protection_status == "software_guard_requested"
        for group in topology.speaker_groups
        for channel in group.channels
    )
    if not software_guard_requested:
        return True
    guard = staged_config.get("software_guard")
    if not isinstance(guard, dict):
        return False
    return (
        staged_config.get("status") == "staged"
        and bool(guard.get("passed"))
        and bool(guard.get("no_load"))
        and bool(guard.get("no_playback"))
    )


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


def _staged_topology_matches(
    topology: OutputTopology,
    staged_config: dict[str, Any],
) -> bool:
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
    return all((
        staged_config.get("status") == "staged",
        staged_topology.get("topology_id") == topology.topology_id,
        staged_hardware.get("device_id") == topology.hardware.device_id,
        staged_hardware.get("card_id") == topology.hardware.card_id,
        staged_hardware.get("physical_output_count")
        == topology.hardware.physical_output_count,
        staged_hardware.get("clock_domain_id") == topology.hardware.clock_domain_id,
        staged_target_signature(staged_config) == topology_target_signature(topology),
    ))


def _staged_config_path(staged_config: dict[str, Any]) -> Path | None:
    config = staged_config.get("config") if isinstance(staged_config, dict) else None
    if not isinstance(config, dict):
        return None
    raw = config.get("path")
    return Path(raw) if isinstance(raw, str) and raw.strip() else None


def _file_sha256(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def startup_load_evidence_fingerprint(
    topology: OutputTopology,
    *,
    staged_config: dict[str, Any] | None = None,
    current_config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Return the current startup-load identity a path-safety proof binds to."""

    staged = staged_config if isinstance(staged_config, dict) else {}
    staged_path = _staged_config_path(staged)
    current_path = Path(current_config_path) if current_config_path else None
    return {
        "topology_id": topology.topology_id,
        "hardware_device_id": topology.hardware.device_id,
        "hardware_card_id": topology.hardware.card_id,
        "hardware_output_count": topology.hardware.physical_output_count,
        "hardware_clock_domain_id": topology.hardware.clock_domain_id,
        "target_signature": topology_target_signature(topology),
        "staged_config_path": str(staged_path or ""),
        "staged_config_sha256": _file_sha256(staged_path),
        "current_config_path": str(current_config_path or ""),
        "current_config_sha256": _file_sha256(current_path),
    }


def validate_startup_load_evidence_binding(
    raw: dict[str, Any],
    topology: OutputTopology,
    *,
    staged_config: dict[str, Any] | None = None,
    current_config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Check that path-safety evidence still matches this startup-load attempt."""

    provenance = raw.get("provenance") if isinstance(raw.get("provenance"), dict) else {}
    expected = startup_load_evidence_fingerprint(
        topology,
        staged_config=staged_config,
        current_config_path=current_config_path,
    )
    checks: dict[str, bool] = {
        "evidence_mode": raw.get("evidence_mode") == "startup_load_preflight",
        "scope": raw.get("scope") == "load_only_no_audio",
        "topology_id": provenance.get("topology_id") == expected["topology_id"],
        "hardware_device_id": (
            provenance.get("hardware_device_id") == expected["hardware_device_id"]
        ),
        "hardware_card_id": (
            provenance.get("hardware_card_id") == expected["hardware_card_id"]
        ),
        "hardware_output_count": (
            provenance.get("hardware_output_count")
            == expected["hardware_output_count"]
        ),
        "hardware_clock_domain_id": (
            provenance.get("hardware_clock_domain_id")
            == expected["hardware_clock_domain_id"]
        ),
        "target_signature": (
            provenance.get("target_signature") == expected["target_signature"]
        ),
        "staged_config_path": (
            provenance.get("staged_config_path") == expected["staged_config_path"]
        ),
        "staged_config_sha256": (
            provenance.get("staged_config_sha256")
            == expected["staged_config_sha256"]
        ),
    }
    if current_config_path is not None:
        checks.update({
            "current_config_path": (
                provenance.get("current_config_path")
                == expected["current_config_path"]
            ),
            "current_config_sha256": (
                provenance.get("current_config_sha256")
                == expected["current_config_sha256"]
            ),
        })

    issues = [
        _issue(
            "blocker",
            "path_safety_evidence_stale",
            (
                "path-safety evidence no longer matches this startup-load "
                f"attempt: {check}"
            ),
        )
        for check, passed in checks.items()
        if not passed
    ]
    matched = not issues
    return {
        "status": "matched" if matched else "stale",
        "matched": matched,
        "checks": checks,
        "issues": issues,
        "expected": expected,
    }


def _staged_candidate_ready(staged_config: dict[str, Any]) -> bool:
    config = staged_config.get("config")
    if not isinstance(config, dict):
        return False
    path = _staged_config_path(staged_config)
    validation = config.get("validation")
    validation_status = (
        validation.get("status")
        if isinstance(validation, dict)
        else None
    )
    return all((
        staged_config.get("status") == "staged",
        path is not None,
        path.exists() if path is not None else False,
        config.get("classification") == "active_startup_candidate",
        bool(config.get("volume_limit_ok")),
        validation_status in {"valid", "missing"},
    ))


def _calibration_level_controlled(calibration_level: dict[str, Any] | None) -> bool:
    if not isinstance(calibration_level, dict):
        return False
    signal = calibration_level.get("test_signal")
    if not isinstance(signal, dict):
        return False
    required = ("requested_level_dbfs", "min_level_dbfs", "max_level_dbfs")
    values: dict[str, float] = {}
    for key in required:
        try:
            values[key] = float(signal.get(key))
        except (TypeError, ValueError):
            return False
    return (
        values["min_level_dbfs"]
        <= values["requested_level_dbfs"]
        <= values["max_level_dbfs"]
        <= -30.0
    )


def _startup_muted_by_candidate(staged_config: dict[str, Any]) -> bool:
    guard = staged_config.get("software_guard")
    if isinstance(guard, dict):
        checks = guard.get("checks")
        if isinstance(checks, dict) and isinstance(checks.get("startup_muted"), bool):
            return bool(checks["startup_muted"])
    path = _staged_config_path(staged_config)
    if path is None or not path.exists():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    # Recognize both mute spellings: the per-role `as_*_startup_mute` of the
    # legacy startup emitter and the per-output `as_out{idx}_commission_mute` of
    # the single-audio-path commissioning emitter (which drops the per-role
    # mute). The primary path above reads software_guard.checks.startup_muted;
    # this text fallback covers physically-protected candidates that carry no
    # software-guard block.
    return (
        ("startup_mute" in text or "commission_mute" in text)
        and "mute: true" in text
    )


def _no_raw_fullrange_by_candidate(staged_config: dict[str, Any]) -> bool:
    guard = staged_config.get("software_guard")
    if isinstance(guard, dict):
        checks = guard.get("checks")
        if isinstance(checks, dict):
            needed = ("protective_highpass", "startup_limiter", "headroom_clamped")
            if all(isinstance(checks.get(key), bool) for key in needed):
                return all(bool(checks[key]) for key in needed)
    config = staged_config.get("config")
    return (
        isinstance(config, dict)
        and config.get("classification") == "active_startup_candidate"
        and bool(config.get("volume_limit_ok"))
    )


def _current_config_summary(current_config_path: str | Path | None) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "path": str(current_config_path) if current_config_path else None,
        "exists": False,
        "readable": False,
        "classification": "missing",
        "protected": False,
        "restore_available": False,
        "issues": [],
    }
    if not current_config_path:
        summary["issues"].append(_issue(
            "blocker",
            "current_config_path_missing",
            "current CamillaDSP config path was not available",
        ))
        return summary
    path = Path(current_config_path)
    summary["exists"] = path.exists()
    if not path.exists():
        summary["issues"].append(_issue(
            "blocker",
            "current_config_missing",
            f"current CamillaDSP config path does not exist: {path}",
        ))
        return summary
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        summary["issues"].append(_issue(
            "blocker",
            "current_config_unreadable",
            f"could not read current CamillaDSP config: {type(exc).__name__}",
        ))
        return summary

    from .environment import classify_camilla_config_text

    classification = classify_camilla_config_text(text)
    summary.update({
        "readable": True,
        "classification": classification.get("classification"),
        "label": classification.get("label"),
        "volume_limit_ok": bool(classification.get("volume_limit_ok")),
        "issues": [
            _normalise_issue(issue)
            for issue in classification.get("issues", [])
            if isinstance(issue, dict)
        ],
    })
    summary["protected"] = (
        classification.get("classification") == "active_startup_candidate"
        and bool(classification.get("volume_limit_ok"))
        and not any(
            issue.get("severity") == "blocker"
            for issue in summary["issues"]
        )
    )
    restore_classifications = {
        "active_startup_candidate",
        "jts_generated_stereo",
        "jts_outputd_stereo",
        "jts_legacy_stereo",
    }
    summary["restore_available"] = (
        bool(summary["readable"])
        and classification.get("classification") in restore_classifications
        and bool(classification.get("volume_limit_ok"))
        and not any(
            issue.get("severity") == "blocker"
            for issue in summary["issues"]
        )
    )
    if not summary["protected"]:
        summary["issues"].append(_issue(
            "warning",
            "rollback_target_restores_previous_profile",
            (
                "current CamillaDSP config is a rollback target only; the "
                "staged active-speaker config owns driver protection before "
                "any tone can play"
            ),
        ))
    return summary


def build_startup_load_path_safety_evidence(
    topology: OutputTopology,
    *,
    staged_config: dict[str, Any] | None = None,
    calibration_level: dict[str, Any] | None = None,
    current_config_path: str | Path | None = None,
    current_config_error: str | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build no-audio hardware-probe evidence for startup-load safety.

    This evidence authorizes only the guarded startup-load preflight. It does
    not claim arbitrary playback is safe before the protected candidate is
    loaded, and it does not emit audio, reload CamillaDSP, or mutate hardware.
    """

    if staged_config is None:
        from .staging import load_staged_startup_config

        staged = load_staged_startup_config()
    else:
        staged = staged_config
    staged = staged if isinstance(staged, dict) else {}
    software_guard_ready = software_guard_ready_for_startup(topology, staged)
    topology_blockers = _topology_blockers(
        topology,
        software_guard_ready=software_guard_ready,
    )
    identity = channel_identity_report(topology)
    assigned = int(identity.get("assigned_channel_count") or 0)
    unverified = int(identity.get("unverified_channel_count") or 0)
    topology_ready = assigned > 0 and unverified == 0 and not topology_blockers
    candidate_ready = (
        _staged_candidate_ready(staged)
        and _staged_topology_matches(topology, staged)
        and software_guard_ready
    )
    level_controlled = _calibration_level_controlled(calibration_level)
    rollback = _current_config_summary(current_config_path)
    if current_config_error:
        rollback["issues"].append(_issue(
            "blocker",
            "current_config_probe_failed",
            f"could not query current CamillaDSP config: {current_config_error}",
        ))
        rollback["protected"] = False
    route_verified = topology_ready and candidate_ready
    protected_by_candidate = candidate_ready
    bypass_disabled = candidate_ready
    active_outputs_muted = candidate_ready and _startup_muted_by_candidate(staged)
    no_raw_fullrange = candidate_ready and _no_raw_fullrange_by_candidate(staged)

    paths = {
        "music_renderers": {
            "route_verified": route_verified,
            "protected_by_active_baseline": protected_by_candidate,
            "bypass_disabled": bypass_disabled,
            "notes": (
                "startup-load preflight: renderer paths are verified against "
                "the staged protected candidate that will be loaded"
            ),
        },
        "tts_cues": {
            "route_verified": route_verified,
            "protected_by_active_baseline": protected_by_candidate,
            "bypass_disabled": bypass_disabled,
            "notes": (
                "startup-load preflight: cue paths are verified against the "
                "staged protected candidate"
            ),
        },
        "correction_sweeps": {
            "route_verified": route_verified,
            "protected_by_active_baseline": protected_by_candidate,
            "level_controlled": level_controlled,
            "bypass_disabled": bypass_disabled,
            "notes": (
                "startup-load preflight only; sweep playback still needs a "
                "separate mic/level-gated flow"
            ),
        },
        "test_tones": {
            "route_verified": route_verified,
            "protected_by_active_baseline": protected_by_candidate,
            "level_controlled": level_controlled,
            "notes": (
                "startup-load preflight only; tone playback remains separately "
                "gated and may still be disabled"
            ),
        },
        "rollback_configs": {
            "route_verified": route_verified,
            "protected_by_active_baseline": protected_by_candidate,
            "rollback_target_available": bool(rollback.get("restore_available")),
            "rollback_target_restore_limited": bool(rollback.get("volume_limit_ok")),
            "rollback_target_protected": bool(rollback.get("protected")),
            "notes": (
                "rollback restores the previous bounded config; it does not "
                "authorize active-speaker test tones"
            ),
        },
        "startup_reload": {
            "active_outputs_muted": active_outputs_muted,
            "protected_by_active_baseline": protected_by_candidate,
            "no_raw_fullrange": no_raw_fullrange,
            "notes": (
                "staged candidate starts muted and protected before any "
                "startup-load attempt"
            ),
        },
    }
    observed_issues = [
        *topology_blockers,
        *[
            _normalise_issue(issue)
            for issue in staged.get("issues", [])
            if isinstance(issue, dict)
        ],
        *[
            _normalise_issue(issue)
            for issue in rollback.get("issues", [])
            if isinstance(issue, dict)
        ],
    ]
    if assigned <= 0:
        observed_issues.append(_issue(
            "blocker",
            "no_assigned_outputs",
            "no saved active-speaker DAC outputs are assigned",
        ))
    if unverified:
        observed_issues.append(_issue(
            "blocker",
            "physical_identity_unverified",
            "assigned DAC outputs must be physically verified",
        ))
    if not _staged_topology_matches(topology, staged):
        observed_issues.append(_issue(
            "blocker",
            "staged_topology_mismatch",
            "staged protected candidate does not match the saved output topology",
        ))
    if not _staged_candidate_ready(staged):
        observed_issues.append(_issue(
            "blocker",
            "staged_candidate_not_ready",
            "staged protected candidate is missing or not a validated active startup config",
        ))
    if not level_controlled:
        observed_issues.append(_issue(
            "blocker",
            "calibration_level_guard_missing",
            "active-speaker calibration level guard was not readable",
        ))
    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": PATH_SAFETY_EVIDENCE_KIND,
        "evidence_source": HARDWARE_PROBE_EVIDENCE_SOURCE,
        "evidence_mode": "startup_load_preflight",
        "scope": "load_only_no_audio",
        "generated_at": generated_at or _utc_now(),
        "paths": paths,
        "provenance": {
            **startup_load_evidence_fingerprint(
                topology,
                staged_config=staged,
                current_config_path=current_config_path,
            ),
            "rollback_classification": rollback.get("classification"),
            "rollback_restore_available": rollback.get("restore_available"),
            "assigned_channel_count": assigned,
            "unverified_channel_count": unverified,
            "software_guard_ready": software_guard_ready,
            "calibration_level_controlled": level_controlled,
        },
        "observed_issues": observed_issues,
        "notes": (
            "Generated by deterministic no-audio inspection. Passing evidence "
            "authorizes only the guarded startup-load preflight; it does not "
            "authorize tones or normal playback."
        ),
    }


def _bool_check(value: Any, path_id: str, check: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ActiveSpeakerConfigError(f"{path_id}.{check} must be boolean")


def evaluate_path_safety_evidence(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ActiveSpeakerConfigError("path safety evidence must be an object")
    if raw.get("artifact_schema_version") != SCHEMA_VERSION:
        raise ActiveSpeakerConfigError("unsupported path safety evidence schema version")
    if raw.get("kind") != PATH_SAFETY_EVIDENCE_KIND:
        raise ActiveSpeakerConfigError("unsupported path safety evidence kind")
    evidence_source = raw.get("evidence_source")
    if evidence_source is None:
        raise ActiveSpeakerConfigError("path safety evidence source is required")
    if evidence_source not in SUPPORTED_EVIDENCE_SOURCES:
        raise ActiveSpeakerConfigError("unsupported path safety evidence source")
    paths = raw.get("paths")
    if not isinstance(paths, dict):
        raise ActiveSpeakerConfigError("paths must be an object")

    issues: list[dict[str, Any]] = []
    path_results: list[dict[str, Any]] = []
    known_ids = {requirement.id for requirement in REQUIRED_PATHS}

    for requirement in REQUIRED_PATHS:
        evidence = paths.get(requirement.id)
        if not isinstance(evidence, dict):
            issues.append({
                "severity": "blocker",
                "path_id": requirement.id,
                "code": "missing_path_evidence",
                "message": f"{requirement.label} evidence is missing",
            })
            path_results.append({
                "id": requirement.id,
                "label": requirement.label,
                "status": "missing",
                "checks": {},
            })
            continue

        checks: dict[str, bool] = {}
        for check in requirement.checks:
            if check not in evidence:
                checks[check] = False
                issues.append({
                    "severity": "blocker",
                    "path_id": requirement.id,
                    "code": f"{check}_missing",
                    "message": f"{requirement.label}: {check} evidence is missing",
                })
                continue
            passed = _bool_check(evidence[check], requirement.id, check)
            checks[check] = passed
            if not passed:
                issues.append({
                    "severity": "blocker",
                    "path_id": requirement.id,
                    "code": f"{check}_not_verified",
                    "message": f"{requirement.label}: {check} is not verified",
                })
        path_results.append({
            "id": requirement.id,
            "label": requirement.label,
            "status": "pass" if all(checks.values()) else "blocked",
            "checks": checks,
            "notes": (
                evidence.get("notes")
                if isinstance(evidence.get("notes"), str)
                else None
            ),
        })

    for path_id in sorted(set(paths) - known_ids):
        issues.append({
            "severity": "warning",
            "path_id": path_id,
            "code": "unknown_path_evidence",
            "message": f"unknown path evidence ignored: {path_id}",
        })

    blocker_count = sum(1 for issue in issues if issue["severity"] == "blocker")
    requirements_met = blocker_count == 0
    hardware_probe_backed = evidence_source == HARDWARE_PROBE_EVIDENCE_SOURCE
    ok_to_load = requirements_met and hardware_probe_backed
    load_gate = (
        "ready"
        if ok_to_load
        else (
            "hardware_probe_required"
            if requirements_met
            else "requirements_blocked"
        )
    )
    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": "jts_active_speaker_path_safety_report",
        "status": "pass" if requirements_met else "blocked",
        "requirements_met": requirements_met,
        "evidence_source": evidence_source,
        "hardware_probe_backed": hardware_probe_backed,
        "ok_to_load_active_config": ok_to_load,
        "load_gate": load_gate,
        "blocker_count": blocker_count,
        "issue_count": len(issues),
        "paths": path_results,
        "issues": issues,
    }
