"""Read-only environment evidence for active-speaker bring-up.

This module gathers facts that are useful before any active crossover config is
loaded: ALSA playback devices, the current CamillaDSP statefile target, the
shape of that config, and optional path-safety evidence. It deliberately has no
CamillaDSP websocket client, no tone playback, and no state mutation.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Sequence

from jasper.camilla_config_contract import (
    DEFAULT_PLAYBACK_DEVICE,
    DEFAULT_VOLUME_LIMIT_DB,
    parse_camilla_devices_config,
)
from jasper.dsp_apply import CamillaConfigValidationResult, validate_camilla_config

from ._common import issue as _issue
from .camilla_yaml import FORBIDDEN_ACTIVE_PLAYBACK_TOKENS
from .path_safety import evaluate_path_safety_evidence
from .profile import ActiveSpeakerConfigError

SCHEMA_VERSION = 1
ENVIRONMENT_REPORT_KIND = "jts_active_speaker_environment_report"
SAFE_PLAYBACK_SCHEMA_VERSION = 1
DEFAULT_CAMILLA_STATEFILE = Path("/var/lib/camilladsp/outputd-statefile.yml")
ALSA_PROBE_TIMEOUT_SEC = 3.0

_CARD_RE = re.compile(
    r"^card\s+(?P<card_index>\d+):\s+"
    r"(?P<card_id>[^\s]+)\s+\[(?P<card_name>[^\]]+)\],\s+"
    r"device\s+(?P<device_index>\d+):\s+"
    r"(?P<device_id>.*?)\s+\[(?P<device_name>[^\]]+)\]"
)
_STATEFILE_CONFIG_RE = re.compile(
    r"^\s*config_path:\s*(?P<path>.+?)\s*$",
    re.MULTILINE,
)
_ACTIVE_SPLIT_RE = re.compile(r"\bsplit_active_(?P<way_count>[23])way\b")
_ACTIVE_OUT_RE = re.compile(
    r"channels:\s*\{\s*in:\s*2\s*,\s*out:\s*(?P<out>\d+)\s*\}"
)
_SOURCE_RE = re.compile(r"^#\s*Source:\s*(?P<source>\S+)\s*$", re.MULTILINE)


Runner = Callable[
    [Sequence[str], float],
    subprocess.CompletedProcess[str],
]


def _default_runner(
    argv: Sequence[str], timeout: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        capture_output=True,
        check=False,
        text=True,
        timeout=timeout,
    )


def parse_aplay_playback_devices(text: str) -> list[dict[str, Any]]:
    """Parse ``aplay -l`` output into stable, JSON-safe device summaries."""

    devices: list[dict[str, Any]] = []
    for raw_line in text.splitlines():
        match = _CARD_RE.match(raw_line.strip())
        if not match:
            continue
        card_id = match.group("card_id")
        device_index = int(match.group("device_index"))
        devices.append(
            {
                "card_index": int(match.group("card_index")),
                "card_id": card_id,
                "card_name": match.group("card_name"),
                "device_index": device_index,
                "device_id": " ".join(match.group("device_id").split()),
                "device_name": match.group("device_name"),
                "suggested_hw_device": f"hw:{card_id},{device_index}",
                "suggested_plughw_device": f"plughw:{card_id},{device_index}",
            }
        )
    return devices


def probe_alsa_playback_devices(
    *,
    runner: Runner = _default_runner,
) -> dict[str, Any]:
    """Return read-only ALSA playback-device evidence."""

    try:
        completed = runner(["aplay", "-l"], ALSA_PROBE_TIMEOUT_SEC)
    except FileNotFoundError:
        return {
            "available": False,
            "command": ["aplay", "-l"],
            "returncode": None,
            "devices": [],
            "issue_count": 1,
            "issues": [
                _issue(
                    "blocker",
                    "aplay_missing",
                    "aplay is not installed or not on PATH; ALSA devices were not probed",
                )
            ],
        }
    except subprocess.TimeoutExpired:
        return {
            "available": False,
            "command": ["aplay", "-l"],
            "returncode": None,
            "devices": [],
            "issue_count": 1,
            "issues": [
                _issue(
                    "blocker",
                    "aplay_timeout",
                    "aplay -l timed out; ALSA devices were not probed",
                )
            ],
        }

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    devices = parse_aplay_playback_devices(stdout)
    issues: list[dict[str, str]] = []
    if completed.returncode != 0:
        issues.append(
            _issue(
                "blocker",
                "aplay_failed",
                f"aplay -l exited {completed.returncode}: {stderr.strip()[:200]}",
            )
        )
    elif not devices:
        issues.append(
            _issue(
                "blocker",
                "no_playback_devices",
                "aplay -l returned no playback hardware devices",
            )
        )

    return {
        "available": completed.returncode == 0,
        "command": ["aplay", "-l"],
        "returncode": completed.returncode,
        "devices": devices,
        "issue_count": len(issues),
        "issues": issues,
    }


def parse_camilla_statefile_config_path(text: str) -> str | None:
    """Extract ``config_path`` from a CamillaDSP statefile."""

    match = _STATEFILE_CONFIG_RE.search(text)
    if not match:
        return None
    return match.group("path").strip().strip("'\"") or None


def _statefile_path(path: str | Path | None) -> Path:
    if path is not None:
        return Path(path)
    return Path(
        os.environ.get("JASPER_CAMILLA_STATEFILE", str(DEFAULT_CAMILLA_STATEFILE))
    )


def _config_path_from_statefile(path: Path) -> tuple[str | None, list[dict[str, str]]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return None, [
            _issue(
                "blocker",
                "camilla_statefile_unreadable",
                f"could not read CamillaDSP statefile {path}: {e}",
            )
        ]
    config_path = parse_camilla_statefile_config_path(text)
    if not config_path:
        return None, [
            _issue(
                "blocker",
                "camilla_statefile_missing_config_path",
                f"CamillaDSP statefile {path} does not contain config_path",
            )
        ]
    return config_path, []


def _source_marker(text: str) -> str | None:
    match = _SOURCE_RE.search(text)
    return match.group("source") if match else None


def _forbidden_playback_token(playback_device: str | None) -> str | None:
    if not playback_device:
        return None
    lowered = playback_device.lower()
    for token in FORBIDDEN_ACTIVE_PLAYBACK_TOKENS:
        if token.lower() in lowered:
            return token
    return None


def _active_split_summary(text: str) -> dict[str, Any]:
    split = _ACTIVE_SPLIT_RE.search(text)
    out: re.Match[str] | None = None
    if split:
        # Generated active templates put the mixer channel declaration
        # immediately under the split mixer. Keep this precise enough that
        # some other mixer cannot satisfy the active-output count by accident.
        remaining = text[split.end() :]
        out = _ACTIVE_OUT_RE.search("\n".join(remaining.splitlines()[:20]))
    return {
        "present": bool(split),
        "way_count": int(split.group("way_count")) if split else None,
        "mixer_output_channels": int(out.group("out")) if out else None,
    }


def classify_camilla_config_text(text: str) -> dict[str, Any]:
    """Classify a CamillaDSP config for active-speaker safety reporting."""

    devices = parse_camilla_devices_config(text)
    playback_device = devices.get("playback_device")
    playback_channels = devices.get("playback_channels")
    volume_limit_db = devices.get("volume_limit")
    source = _source_marker(text)
    split = _active_split_summary(text)
    active_startup_marker = (
        "Auto-generated active-speaker startup config" in text
        or "jasper.active_speaker.camilla_yaml.emit_active_speaker_startup_config"
        in text
    )

    if active_startup_marker or split["present"]:
        classification = "active_startup_candidate"
        label = "JTS active-speaker startup candidate"
    elif source in {
        "jasper.sound.camilla_yaml.emit_sound_config",
        "jasper.correction.camilla_yaml.emit_correction_config",
    }:
        classification = "jts_generated_stereo"
        label = "JTS generated stereo DSP config"
    elif playback_device == DEFAULT_PLAYBACK_DEVICE:
        classification = "jts_outputd_stereo"
        label = "JTS outputd stereo config"
    elif playback_device and "jasper_out" in playback_device:
        classification = "jts_legacy_stereo"
        label = "JTS legacy stereo output config"
    else:
        classification = "unknown_custom"
        label = "Advanced DSP config active; JTS cannot safely preserve this"

    issues: list[dict[str, str]] = []
    if volume_limit_db is None:
        issues.append(
            _issue(
                "blocker",
                "volume_limit_missing",
                "CamillaDSP config omits devices.volume_limit; CamillaDSP defaults above 0 dB",
            )
        )
    elif volume_limit_db > DEFAULT_VOLUME_LIMIT_DB:
        issues.append(
            _issue(
                "blocker",
                "volume_limit_positive",
                (
                    f"CamillaDSP config sets devices.volume_limit={volume_limit_db:.1f} dB; "
                    f"expected <= {DEFAULT_VOLUME_LIMIT_DB:.1f} dB"
                ),
            )
        )

    forbidden = _forbidden_playback_token(playback_device)
    if classification == "active_startup_candidate" and forbidden:
        issues.append(
            _issue(
                "blocker",
                "active_config_uses_jts_stereo_playback_lane",
                (
                    "active-speaker config targets the existing JTS stereo playback "
                    f"lane ({forbidden}), not explicit active hardware"
                ),
            )
        )

    if classification == "active_startup_candidate":
        mixer_output_channels = split["mixer_output_channels"]
        if playback_channels is None:
            issues.append(
                _issue(
                    "blocker",
                    "active_playback_channels_missing",
                    "active-speaker config must declare devices.playback.channels",
                )
            )
        if not split["present"]:
            issues.append(
                _issue(
                    "blocker",
                    "active_split_missing",
                    "active-speaker config must include a split_active_2way or split_active_3way mixer",
                )
            )
        elif mixer_output_channels is None:
            issues.append(
                _issue(
                    "blocker",
                    "active_split_output_channels_missing",
                    "active-speaker split mixer must declare channels: { in: 2, out: N }",
                )
            )
        elif playback_channels is not None and playback_channels != mixer_output_channels:
            issues.append(
                _issue(
                    "blocker",
                    "active_playback_channels_mismatch",
                    (
                        "active-speaker playback channel count "
                        f"({playback_channels}) does not match split mixer outputs "
                        f"({mixer_output_channels})"
                    ),
                )
            )

    if classification == "unknown_custom":
        issues.append(
            _issue(
                "blocker",
                "unknown_custom_camilla_config",
                (
                    "advanced/custom CamillaDSP config is active; JTS will not "
                    "overwrite it through the guided active-speaker flow"
                ),
            )
        )

    if playback_channels is not None and playback_channels < 2:
        issues.append(
            _issue(
                "blocker",
                "playback_channels_too_low",
                f"CamillaDSP playback has {playback_channels} channel(s); expected at least 2",
            )
        )

    return {
        "classification": classification,
        "label": label,
        "source": source,
        "devices": devices,
        "active_split": split,
        "playback_device": playback_device,
        "playback_channels": playback_channels,
        "volume_limit_db": volume_limit_db,
        "volume_limit_ok": (
            volume_limit_db is not None and volume_limit_db <= DEFAULT_VOLUME_LIMIT_DB
        ),
        "issues": issues,
    }


def _read_config_summary(
    *,
    config_path: str | Path | None,
    statefile_path: str | Path | None,
) -> dict[str, Any]:
    statefile = _statefile_path(statefile_path)
    issues: list[dict[str, str]] = []
    path_source = "argument"
    resolved_config_path = str(config_path) if config_path else None
    if not resolved_config_path:
        path_source = "statefile"
        resolved_config_path, statefile_issues = _config_path_from_statefile(statefile)
        issues.extend(statefile_issues)

    summary: dict[str, Any] = {
        "statefile_path": str(statefile),
        "path_source": path_source,
        "path": resolved_config_path,
        "exists": False,
        "readable": False,
        "classification": "missing",
        "label": "CamillaDSP config missing",
        "devices": {},
        "active_split": {
            "present": False,
            "way_count": None,
            "mixer_output_channels": None,
        },
        "issues": issues,
    }

    if not resolved_config_path:
        return summary

    path = Path(resolved_config_path)
    summary["exists"] = path.exists()
    if not path.exists():
        summary["issues"].append(
            _issue(
                "blocker",
                "camilla_config_missing",
                f"CamillaDSP config path does not exist: {path}",
            )
        )
        return summary

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        summary["issues"].append(
            _issue(
                "blocker",
                "camilla_config_unreadable",
                f"could not read CamillaDSP config {path}: {e}",
            )
        )
        return summary

    classification = classify_camilla_config_text(text)
    summary.update(classification)
    summary["readable"] = True
    summary["issues"] = [*summary["issues"], *classification["issues"]]
    return summary


def _validation_payload(
    path: str | None,
    *,
    run_config_check: bool,
    validate: Callable[[str | Path], CamillaConfigValidationResult],
) -> dict[str, Any]:
    if not path:
        return {"status": "skipped", "reason": "no_config_path"}
    if not run_config_check:
        return {"status": "skipped", "reason": "disabled"}
    return validate(path).to_dict()


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
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        return {
            "provided": True,
            "path": str(path),
            "status": "unreadable",
            "ok_to_load_active_config": False,
            "load_gate": "evidence_unreadable",
            "issues": [
                _issue(
                    "blocker",
                    "path_safety_evidence_unreadable",
                    f"could not read active-speaker path-safety evidence: {e}",
                )
            ],
        }
    try:
        payload = json.loads(raw)
        report = evaluate_path_safety_evidence(payload)
    except (json.JSONDecodeError, ActiveSpeakerConfigError) as e:
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
                    f"invalid active-speaker path-safety evidence: {e}",
                )
            ],
        }
    report["provided"] = True
    report["path"] = str(path)
    return report


def _combine_issues(*sections: dict[str, Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for section in sections:
        for issue in section.get("issues", []):
            if isinstance(issue, dict):
                out.append(
                    {
                        "severity": str(issue.get("severity", "warning")),
                        "code": str(issue.get("code", "unknown_issue")),
                        "message": str(
                            issue.get("message", issue.get("code", "issue"))
                        ),
                    }
                )
    return out


def _safe_playback_payload(
    *,
    ok_to_load_active_config: bool,
    load_gate: str,
    camilla_config: dict[str, Any],
    path_safety: dict[str, Any],
) -> dict[str, Any]:
    """Describe the current sound-emitting boundary without authorizing audio.

    This report is intentionally conservative. It gives the UI and future
    harness code a stable place to attach the safe-playback path while keeping
    today's environment probe read-only.
    """

    required_gates = [
        {
            "id": "active_startup_candidate",
            "passed": camilla_config.get("classification")
            == "active_startup_candidate",
            "label": "CamillaDSP config is a JTS active-speaker startup candidate",
        },
        {
            "id": "validated_config",
            "passed": ok_to_load_active_config,
            "label": "Config, ALSA, and path-safety load gate are ready",
        },
        {
            "id": "hardware_probe_path_safety",
            "passed": bool(path_safety.get("ok_to_load_active_config")),
            "label": "Path-safety evidence is hardware-probe-backed",
        },
        {
            "id": "physical_channel_identity",
            "passed": False,
            "label": "Physical output channels have been identified before drivers are connected",
        },
        {
            "id": "level_limited_tone_generator",
            "passed": False,
            "label": "Level-limited, band-limited tone generator with emergency stop is implemented",
        },
    ]
    return {
        "artifact_schema_version": SAFE_PLAYBACK_SCHEMA_VERSION,
        "status": "not_implemented",
        "playback_allowed": False,
        "load_gate": load_gate,
        "required_gates": required_gates,
        "next_step": (
            "Build physical channel identification and level-limited "
            "test-tone playback only after the active config load gate is ready."
        ),
        "warning": (
            "This probe does not play tones, reload CamillaDSP, or authorize "
            "active-speaker audio output."
        ),
    }


def probe_active_speaker_environment(
    *,
    config_path: str | Path | None = None,
    statefile_path: str | Path | None = None,
    path_safety_evidence_path: str | Path | None = None,
    run_config_check: bool = True,
    runner: Runner = _default_runner,
    validate: Callable[
        [str | Path], CamillaConfigValidationResult
    ] = validate_camilla_config,
) -> dict[str, Any]:
    """Build a versioned, read-only active-speaker environment report."""

    alsa = probe_alsa_playback_devices(runner=runner)
    camilla_config = _read_config_summary(
        config_path=config_path,
        statefile_path=statefile_path,
    )
    validation = _validation_payload(
        camilla_config.get("path"),
        run_config_check=run_config_check,
        validate=validate,
    )
    path_safety = _path_safety_payload(path_safety_evidence_path)

    load_blockers = _combine_issues(alsa, camilla_config, path_safety)
    validation_status = validation.get("status")
    if validation_status not in {"valid"}:
        load_blockers.append(
            _issue(
                "blocker",
                "camilla_config_not_validated",
                (
                    "CamillaDSP config has not been validated by camilladsp --check; "
                    f"validation status is {validation_status}"
                ),
            )
        )
    if camilla_config.get("classification") != "active_startup_candidate":
        load_blockers.append(
            _issue(
                "blocker",
                "active_startup_candidate_required",
                "current/provided CamillaDSP config is not an active-speaker startup candidate",
            )
        )
    if path_safety.get("provided") and not path_safety.get("ok_to_load_active_config"):
        load_blockers.append(
            _issue(
                "blocker",
                "path_safety_load_gate_not_ready",
                (
                    "path-safety evidence does not authorize active config loading; "
                    f"gate is {path_safety.get('load_gate', 'unknown')}"
                ),
            )
        )

    blocker_count = sum(
        1 for issue in load_blockers if issue.get("severity") == "blocker"
    )
    ok_to_load = (
        blocker_count == 0
        and bool(path_safety.get("ok_to_load_active_config"))
        and camilla_config.get("classification") == "active_startup_candidate"
    )
    if ok_to_load:
        load_gate = "ready"
    elif not path_safety.get("provided"):
        load_gate = "path_safety_evidence_missing"
    elif not path_safety.get("ok_to_load_active_config"):
        load_gate = str(path_safety.get("load_gate") or "path_safety_blocked")
    else:
        load_gate = "environment_blocked"
    safe_playback = _safe_playback_payload(
        ok_to_load_active_config=ok_to_load,
        load_gate=load_gate,
        camilla_config=camilla_config,
        path_safety=path_safety,
    )

    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": ENVIRONMENT_REPORT_KIND,
        "status": "pass" if ok_to_load else "blocked",
        "ok_to_load_active_config": ok_to_load,
        "load_gate": load_gate,
        "blocker_count": blocker_count,
        "issue_count": len(load_blockers),
        "alsa": alsa,
        "camilla_config": camilla_config,
        "camilla_validation": validation,
        "path_safety": path_safety,
        "safe_playback": safe_playback,
        "issues": load_blockers,
    }
