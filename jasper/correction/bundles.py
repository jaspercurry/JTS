"""Correction session-bundle helpers.

Session bundles are the replay boundary for future FIR and agent work.
Keep their discovery and validation here so the web handler, doctor,
and CLI tools do not each grow their own partial JSON parser.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CURRENT_BUNDLE_SCHEMA_VERSION = 2


class BundleError(RuntimeError):
    """A session bundle is missing or malformed."""


@dataclass(frozen=True)
class BundleIssue:
    code: str
    severity: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
        }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except OSError as e:
        raise BundleError(f"could not read {path.name}: {e}") from e
    except json.JSONDecodeError as e:
        raise BundleError(f"{path.name} is invalid JSON: {e.msg}") from e
    if not isinstance(data, dict):
        raise BundleError(f"{path.name} must be a JSON object")
    return data


def summarize_bundle(bundle_dir: Path) -> dict[str, Any]:
    """Return info.json plus derived artifact flags for one bundle."""
    if not bundle_dir.is_dir():
        raise BundleError(f"{bundle_dir} is not a directory")
    info = _read_json(bundle_dir / "info.json")
    info["bundle_dir"] = str(bundle_dir)
    info["has_result"] = (bundle_dir / "result.json").exists()
    info["has_applied_yml"] = (bundle_dir / "applied.yml").exists()
    info["has_verify_wav"] = (bundle_dir / "verify.wav").exists()
    info["has_mic_calibration_json"] = (
        bundle_dir / "mic_calibration.json"
    ).exists()
    info["has_mic_calibration_txt"] = (
        bundle_dir / "mic_calibration.txt"
    ).exists()
    return info


def list_bundles(
    sessions_dir: Path,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """List parseable bundles newest-first, skipping partial writes."""
    if not sessions_dir.is_dir():
        return []
    entries: list[dict[str, Any]] = []
    for sub in sessions_dir.iterdir():
        if not sub.is_dir() or not (sub / "info.json").exists():
            continue
        try:
            entries.append(summarize_bundle(sub))
        except BundleError:
            continue
    entries.sort(key=lambda e: e.get("started_at", 0), reverse=True)
    return entries[:limit]


def validate_bundle(bundle_dir: Path) -> list[BundleIssue]:
    """Validate the bundle contract enough for doctor/agent intake."""
    issues: list[BundleIssue] = []
    try:
        info = _read_json(bundle_dir / "info.json")
    except BundleError as e:
        return [BundleIssue("info_json", "fail", str(e))]

    schema = info.get("bundle_schema_version")
    if schema != CURRENT_BUNDLE_SCHEMA_VERSION:
        issues.append(BundleIssue(
            "schema_version",
            "warn",
            f"bundle schema {schema!r}; expected {CURRENT_BUNDLE_SCHEMA_VERSION}",
        ))
    state = info.get("state")
    if not info.get("session_id"):
        issues.append(BundleIssue(
            "session_id",
            "fail",
            "info.json missing session_id",
        ))
    if not state:
        issues.append(BundleIssue("state", "fail", "info.json missing state"))
    elif state == "failed":
        detail = info.get("error") or "no error recorded"
        issues.append(BundleIssue(
            "session_failed",
            "warn",
            f"bundle state=failed: {detail}",
        ))

    result_path = bundle_dir / "result.json"
    if state in {"ready", "applied", "verified"}:
        if not result_path.exists():
            issues.append(BundleIssue(
                "result_json_missing",
                "warn",
                f"state={info.get('state')} but result.json is missing",
            ))
        else:
            try:
                result = _read_json(result_path)
                if (
                    result.get("bundle_schema_version")
                    != CURRENT_BUNDLE_SCHEMA_VERSION
                ):
                    issues.append(BundleIssue(
                        "result_schema_version",
                        "warn",
                        "result.json schema does not match current bundle schema",
                    ))
            except BundleError as e:
                issues.append(BundleIssue("result_json", "fail", str(e)))

    mic = info.get("mic_calibration")
    if mic:
        if not (bundle_dir / "mic_calibration.json").exists():
            issues.append(BundleIssue(
                "mic_calibration_json_missing",
                "warn",
                "mic_calibration metadata present but mic_calibration.json missing",
            ))
        if not (bundle_dir / "mic_calibration.txt").exists():
            issues.append(BundleIssue(
                "mic_calibration_txt_missing",
                "warn",
                "mic_calibration metadata present but raw calibration file missing",
            ))

    reports = list(info.get("capture_quality") or [])
    if info.get("verify_quality"):
        reports.append(info["verify_quality"])
    for report in reports:
        if not isinstance(report, dict):
            continue
        for issue in report.get("issues") or []:
            if not isinstance(issue, dict):
                continue
            severity = issue.get("severity")
            if severity in {"warn", "fail"}:
                issues.append(BundleIssue(
                    str(issue.get("code") or "capture_quality"),
                    severity,
                    str(issue.get("message") or "capture quality issue"),
                ))
    return issues


def latest_bundle(sessions_dir: Path) -> dict[str, Any] | None:
    bundles = list_bundles(sessions_dir, limit=1)
    return bundles[0] if bundles else None
