"""Deterministic safety gates for active-speaker audible paths.

Before JTS can load an active crossover onto real hardware, every
audible source path must be proven to enter the protected active
baseline. This module encodes that checklist as data and evaluates
operator or future hardware-probe evidence. It does not inspect ALSA,
systemd, or CamillaDSP directly yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .profile import ActiveSpeakerConfigError

SCHEMA_VERSION = 1
PATH_SAFETY_EVIDENCE_KIND = "jts_active_speaker_path_safety_evidence"
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
            "rollback_target_protected",
        ),
        why=(
            "Resetting to a stereo identity config can send full-range "
            "content to a tweeter."
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


def requirements_payload() -> dict[str, Any]:
    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": "jts_active_speaker_path_safety_requirements",
        "requirements": [path.to_dict() for path in REQUIRED_PATHS],
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
