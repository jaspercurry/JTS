"""Correction session-bundle helpers.

Session bundles are the replay boundary for future FIR and agent work.
Keep their discovery and validation here so the web handler, doctor,
and CLI tools do not each grow their own partial JSON parser.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

CURRENT_BUNDLE_SCHEMA_VERSION = 3
CURRENT_ARTIFACT_MANIFEST_VERSION = 1
ARTIFACT_MANIFEST_NAME = "artifact_manifest.json"

logger = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class ArtifactEntry:
    path: str
    kind: str
    sensitivity: str
    recomputable: bool
    sha256: str
    byte_size: int
    recorded_at: float
    generated_by: str
    dependencies: tuple[str, ...] = ()
    schema_version: int | None = None
    metadata: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "path": self.path,
            "kind": self.kind,
            "sensitivity": self.sensitivity,
            "recomputable": self.recomputable,
            "sha256": self.sha256,
            "byte_size": self.byte_size,
            "recorded_at": self.recorded_at,
            "generated_by": self.generated_by,
            "dependencies": list(self.dependencies),
        }
        if self.schema_version is not None:
            out["schema_version"] = self.schema_version
        if self.metadata:
            out["metadata"] = self.metadata
        return out


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


def _write_json_atomically(
    path: Path,
    payload: dict[str, Any],
    *,
    file_mode: int | None = None,
) -> None:
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, default=str))
    if file_mode is not None:
        tmp_path.chmod(file_mode)
    tmp_path.replace(path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_artifact_path(bundle_dir: Path, artifact_path: Path | str) -> str:
    bundle_root = bundle_dir.resolve()
    path = Path(artifact_path)
    if not path.is_absolute():
        path = bundle_dir / path
    try:
        rel = path.resolve().relative_to(bundle_root)
    except ValueError as e:
        raise BundleError(f"artifact path {artifact_path!s} is outside bundle") from e
    if rel.name == ARTIFACT_MANIFEST_NAME:
        raise BundleError("artifact_manifest.json cannot list itself")
    if any(part in {"", ".", ".."} for part in rel.parts):
        raise BundleError(f"artifact path {artifact_path!s} is not normalized")
    return rel.as_posix()


def _safe_manifest_dependencies(
    bundle_dir: Path,
    dependencies: Iterable[str],
) -> tuple[str, ...]:
    out: list[str] = []
    for dep in dependencies:
        try:
            out.append(_relative_artifact_path(bundle_dir, dep))
        except BundleError:
            logger.warning(
                "event=correction_bundle_dependency_ignored path=%s",
                dep,
            )
    return tuple(sorted(set(out)))


def _manifest_path(bundle_dir: Path) -> Path:
    return bundle_dir / ARTIFACT_MANIFEST_NAME


def _read_manifest_artifacts(bundle_dir: Path) -> list[dict[str, Any]]:
    manifest_path = _manifest_path(bundle_dir)
    if not manifest_path.exists():
        return []
    data = _read_json(manifest_path)
    artifacts = data.get("artifacts")
    if not isinstance(artifacts, list):
        raise BundleError("artifact_manifest.json artifacts must be a list")
    return [a for a in artifacts if isinstance(a, dict)]


def read_artifact_manifest(bundle_dir: Path) -> dict[str, Any]:
    """Read artifact_manifest.json for callers that need raw details."""
    return _read_json(_manifest_path(bundle_dir))


def record_artifact(
    bundle_dir: Path,
    artifact_path: Path | str,
    *,
    kind: str,
    sensitivity: str,
    recomputable: bool,
    generated_by: str,
    dependencies: Iterable[str] = (),
    schema_version: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record one bundle artifact in artifact_manifest.json.

    The manifest is an integrity surface, not a transaction log: entries
    are upserted by relative path, so frequently rewritten artifacts
    such as info.json keep one current checksum.
    """
    bundle_dir.mkdir(parents=True, exist_ok=True)
    rel_path = _relative_artifact_path(bundle_dir, artifact_path)
    path = bundle_dir / rel_path
    try:
        stat = path.stat()
    except OSError as e:
        raise BundleError(f"artifact {rel_path} cannot be stat'ed: {e}") from e

    try:
        artifacts = _read_manifest_artifacts(bundle_dir)
    except BundleError as e:
        logger.warning(
            "event=correction_bundle_manifest_reset bundle=%s error=%s",
            bundle_dir,
            e,
        )
        artifacts = []

    entry = ArtifactEntry(
        path=rel_path,
        kind=kind,
        sensitivity=sensitivity,
        recomputable=bool(recomputable),
        sha256=_sha256_file(path),
        byte_size=stat.st_size,
        recorded_at=time.time(),
        generated_by=generated_by,
        dependencies=_safe_manifest_dependencies(bundle_dir, dependencies),
        schema_version=schema_version,
        metadata=metadata,
    ).to_dict()
    by_path: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        raw_path = artifact.get("path")
        if not isinstance(raw_path, str):
            continue
        try:
            existing_rel_path = _relative_artifact_path(bundle_dir, raw_path)
        except BundleError:
            logger.warning(
                "event=correction_bundle_manifest_entry_dropped path=%s",
                raw_path,
            )
            continue
        by_path[existing_rel_path] = {**artifact, "path": existing_rel_path}
    by_path[rel_path] = entry
    manifest = {
        "manifest_schema_version": CURRENT_ARTIFACT_MANIFEST_VERSION,
        "bundle_schema_version": CURRENT_BUNDLE_SCHEMA_VERSION,
        "generated_at": time.time(),
        "artifacts": [
            by_path[path]
            for path in sorted(by_path)
        ],
    }
    _write_json_atomically(_manifest_path(bundle_dir), manifest)
    return entry


def write_json_artifact(
    bundle_dir: Path,
    relative_path: str,
    payload: dict[str, Any],
    *,
    kind: str,
    sensitivity: str,
    recomputable: bool,
    generated_by: str,
    dependencies: Iterable[str] = (),
    schema_version: int | None = None,
    file_mode: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Atomically write JSON and update its manifest entry."""
    rel_path = _relative_artifact_path(bundle_dir, relative_path)
    target_path = bundle_dir / rel_path
    target_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomically(target_path, payload, file_mode=file_mode)
    record_artifact(
        bundle_dir,
        rel_path,
        kind=kind,
        sensitivity=sensitivity,
        recomputable=recomputable,
        generated_by=generated_by,
        dependencies=dependencies,
        schema_version=schema_version,
        metadata=metadata,
    )


def summarize_bundle(bundle_dir: Path) -> dict[str, Any]:
    """Return info.json plus derived artifact flags for one bundle."""
    if not bundle_dir.is_dir():
        raise BundleError(f"{bundle_dir} is not a directory")
    info = _read_json(bundle_dir / "info.json")
    info["bundle_dir"] = str(bundle_dir)
    info["has_result"] = (bundle_dir / "result.json").exists()
    info["has_applied_yml"] = (bundle_dir / "applied.yml").exists()
    info["has_verify_wav"] = (bundle_dir / "verify.wav").exists()
    info["noise_capture_count"] = len(
        [p for p in (bundle_dir / "noise").glob("*.wav")]
        if (bundle_dir / "noise").exists()
        else []
    )
    info["repeat_capture_count"] = len(
        [p for p in (bundle_dir / "repeat_captures").glob("*.wav")]
        if (bundle_dir / "repeat_captures").exists()
        else []
    )
    info["has_runtime_integrity_json"] = (
        bundle_dir / "runtime_integrity.json"
    ).exists()
    info["has_acoustic_quality_json"] = (
        bundle_dir / "acoustic_quality.json"
    ).exists()
    info["has_mic_calibration_json"] = (
        bundle_dir / "mic_calibration.json"
    ).exists()
    info["has_mic_calibration_txt"] = (
        bundle_dir / "mic_calibration.txt"
    ).exists()
    manifest_path = _manifest_path(bundle_dir)
    info["has_artifact_manifest"] = manifest_path.exists()
    if manifest_path.exists():
        try:
            manifest = _read_json(manifest_path)
            artifacts = manifest.get("artifacts")
            info["artifact_count"] = (
                len(artifacts) if isinstance(artifacts, list) else 0
            )
        except BundleError:
            info["artifact_count"] = 0
            info["artifact_manifest_error"] = True
    else:
        info["artifact_count"] = 0
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
    _validate_artifact_manifest(
        bundle_dir,
        issues,
        require_manifest=(schema == CURRENT_BUNDLE_SCHEMA_VERSION),
    )
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

    runtime_path = bundle_dir / "runtime_integrity.json"
    runtime_summary = info.get("runtime_integrity")
    if runtime_summary and not runtime_path.exists():
        issues.append(BundleIssue(
            "runtime_integrity_json_missing",
            "warn",
            "runtime_integrity summary present but runtime_integrity.json missing",
        ))
    if runtime_path.exists():
        try:
            runtime = _read_json(runtime_path)
            if runtime.get("artifact_schema_version") != 1:
                issues.append(BundleIssue(
                    "runtime_integrity_schema_version",
                    "warn",
                    "runtime_integrity.json schema does not match current version",
                ))
            for issue in runtime.get("issues") or []:
                if not isinstance(issue, dict):
                    continue
                severity = issue.get("severity")
                if severity in {"warn", "fail"}:
                    issues.append(BundleIssue(
                        str(issue.get("code") or "runtime_integrity"),
                        severity,
                        str(issue.get("message") or "runtime integrity issue"),
                    ))
        except BundleError as e:
            issues.append(BundleIssue("runtime_integrity_json", "fail", str(e)))

    acoustic_path = bundle_dir / "acoustic_quality.json"
    acoustic_summary = info.get("acoustic_quality")
    if acoustic_summary and not acoustic_path.exists():
        issues.append(BundleIssue(
            "acoustic_quality_json_missing",
            "warn",
            "acoustic_quality summary present but acoustic_quality.json missing",
        ))
    if acoustic_path.exists():
        try:
            acoustic = _read_json(acoustic_path)
            if acoustic.get("artifact_schema_version") != 1:
                issues.append(BundleIssue(
                    "acoustic_quality_schema_version",
                    "warn",
                    "acoustic_quality.json schema does not match current version",
                ))
            for issue in acoustic.get("issues") or []:
                if not isinstance(issue, dict):
                    continue
                severity = issue.get("severity")
                if severity in {"warn", "fail"}:
                    issues.append(BundleIssue(
                        str(issue.get("code") or "acoustic_quality"),
                        severity,
                        str(issue.get("message") or "acoustic quality issue"),
                    ))
        except BundleError as e:
            issues.append(BundleIssue("acoustic_quality_json", "fail", str(e)))

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


def _validate_artifact_manifest(
    bundle_dir: Path,
    issues: list[BundleIssue],
    *,
    require_manifest: bool,
) -> None:
    manifest_path = _manifest_path(bundle_dir)
    if not manifest_path.exists():
        if require_manifest:
            issues.append(BundleIssue(
                "artifact_manifest_missing",
                "warn",
                "current-schema bundle is missing artifact_manifest.json",
            ))
        return
    try:
        manifest = _read_json(manifest_path)
    except BundleError as e:
        issues.append(BundleIssue("artifact_manifest", "fail", str(e)))
        return

    if (
        manifest.get("manifest_schema_version")
        != CURRENT_ARTIFACT_MANIFEST_VERSION
    ):
        issues.append(BundleIssue(
            "artifact_manifest_schema_version",
            "warn",
            "artifact_manifest.json schema does not match current version",
        ))

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        issues.append(BundleIssue(
            "artifact_manifest_artifacts",
            "fail",
            "artifact_manifest.json artifacts must be a list",
        ))
        return

    entries: dict[str, dict[str, Any]] = {}
    for idx, entry in enumerate(artifacts):
        if not isinstance(entry, dict):
            issues.append(BundleIssue(
                "artifact_manifest_entry",
                "fail",
                f"artifact_manifest.json entry {idx} must be an object",
            ))
            continue
        raw_path = entry.get("path")
        if not isinstance(raw_path, str):
            issues.append(BundleIssue(
                "artifact_manifest_entry_path",
                "fail",
                f"artifact_manifest.json entry {idx} missing path",
            ))
            continue
        try:
            rel_path = _relative_artifact_path(bundle_dir, raw_path)
        except BundleError as e:
            issues.append(BundleIssue(
                "artifact_manifest_entry_path",
                "fail",
                str(e),
            ))
            continue
        if rel_path in entries:
            issues.append(BundleIssue(
                "artifact_manifest_duplicate_path",
                "warn",
                f"artifact_manifest.json repeats {rel_path}",
            ))
        entries[rel_path] = entry

        for field in (
            "kind",
            "sensitivity",
            "recomputable",
            "sha256",
            "byte_size",
            "generated_by",
        ):
            if field not in entry:
                issues.append(BundleIssue(
                    "artifact_manifest_entry_field",
                    "warn",
                    f"{rel_path} missing manifest field {field}",
                ))

        path = bundle_dir / rel_path
        if not path.exists():
            issues.append(BundleIssue(
                "artifact_missing",
                "fail",
                f"manifest lists {rel_path}, but the file is missing",
            ))
            continue
        expected_size = entry.get("byte_size")
        if isinstance(expected_size, int) and expected_size != path.stat().st_size:
            issues.append(BundleIssue(
                "artifact_size_mismatch",
                "fail",
                f"{rel_path} byte_size changed since manifest record",
            ))
        expected_sha = entry.get("sha256")
        if isinstance(expected_sha, str) and expected_sha != _sha256_file(path):
            issues.append(BundleIssue(
                "artifact_sha256_mismatch",
                "fail",
                f"{rel_path} sha256 changed since manifest record",
            ))

    _validate_manifest_dependencies(entries, issues)
    if require_manifest:
        _validate_manifest_covers_existing_core_artifacts(
            bundle_dir,
            entries,
            issues,
        )


def _validate_manifest_dependencies(
    entries: dict[str, dict[str, Any]],
    issues: list[BundleIssue],
) -> None:
    for rel_path, entry in entries.items():
        dependencies = entry.get("dependencies")
        if dependencies is None:
            continue
        if not isinstance(dependencies, list):
            issues.append(BundleIssue(
                "artifact_dependencies",
                "warn",
                f"{rel_path} dependencies must be a list",
            ))
            continue
        for dep in dependencies:
            if not isinstance(dep, str):
                issues.append(BundleIssue(
                    "artifact_dependency",
                    "warn",
                    f"{rel_path} has a non-string dependency",
                ))
                continue
            if dep not in entries:
                issues.append(BundleIssue(
                    "artifact_dependency_missing",
                    "warn",
                    f"{rel_path} depends on {dep}, which is not in the manifest",
                ))


def _validate_manifest_covers_existing_core_artifacts(
    bundle_dir: Path,
    entries: dict[str, dict[str, Any]],
    issues: list[BundleIssue],
) -> None:
    core_paths = [
        "info.json",
        "result.json",
        "position_analysis.json",
        "runtime_integrity.json",
        "acoustic_quality.json",
        "mic_calibration.json",
        "mic_calibration.txt",
        "applied.yml",
        "verify.wav",
    ]
    core_paths.extend(
        path.relative_to(bundle_dir).as_posix()
        for path in sorted((bundle_dir / "captures").glob("*.wav"))
        if path.is_file()
    )
    core_paths.extend(
        path.relative_to(bundle_dir).as_posix()
        for path in sorted((bundle_dir / "noise").glob("*.wav"))
        if path.is_file()
    )
    core_paths.extend(
        path.relative_to(bundle_dir).as_posix()
        for path in sorted((bundle_dir / "repeat_captures").glob("*.wav"))
        if path.is_file()
    )
    for rel_path in core_paths:
        if (bundle_dir / rel_path).exists() and rel_path not in entries:
            issues.append(BundleIssue(
                "artifact_manifest_missing_entry",
                "warn",
                f"{rel_path} exists but is not listed in artifact_manifest.json",
            ))


def latest_bundle(sessions_dir: Path) -> dict[str, Any] | None:
    bundles = list_bundles(sessions_dir, limit=1)
    return bundles[0] if bundles else None
