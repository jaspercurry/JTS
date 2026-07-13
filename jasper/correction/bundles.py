# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Correction session-bundle helpers.

Session bundles are the replay boundary for future FIR and agent work.
Keep their discovery and validation here so the web handler, doctor,
and CLI tools do not each grow their own partial JSON parser.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from jasper.audio_measurement.bundles import (
    ARTIFACT_MANIFEST_NAME as ARTIFACT_MANIFEST_NAME,
    CURRENT_ARTIFACT_MANIFEST_VERSION as CURRENT_ARTIFACT_MANIFEST_VERSION,
    ArtifactEntry as ArtifactEntry,
    BundleError,
    _is_exact_version,
    _manifest_path,
    _read_json,
    _relative_artifact_path,
    read_artifact_manifest as read_artifact_manifest,
    record_artifact as _record_artifact,
    sha256_file as _sha256_file,
    write_json_artifact as _write_json_artifact,
)

# v4 (P4): result.json / info.json / status gain the deterministic
# `acceptance` verdict block and the `auto_revert_outcome` rollback record,
# and result.json gains the `position1` matched-basis curve. Additive — older
# readers ignore the new keys.
CURRENT_BUNDLE_SCHEMA_VERSION = 5
RAW_AUDIO_RELATIVE_PATHS = ("verify.wav",)
RAW_AUDIO_DIRS = ("captures", "noise", "repeat_captures")

# On the frequent default `validate_bundle` path — jasper-doctor, the
# evidence packet (jasper.correction.evidence), and agent intake all run
# it across every bundle — skip the full SHA-256 re-hash for artifacts
# larger than this. Raw capture WAVs are ~2 MB each and a bundle holds
# several; re-hashing them all on every run is unbounded CPU/I/O for
# little gain — the artifacts are immutable once written and the cheap
# byte_size equality check (a single stat()) still catches truncation/
# size-changing corruption. Only the on-demand forensic CLI
# (`jasper-correction-bundle inspect`) passes max_sha_verify_bytes=None
# to force a full hash check of every artifact.
DEFAULT_MAX_SHA_VERIFY_BYTES = 1 * 1024 * 1024

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


def _bundle_byte_size(bundle_dir: Path) -> int:
    total = 0
    for path in bundle_dir.rglob("*"):
        if path.is_file():
            try:
                total += path.stat().st_size
            except OSError:
                continue
    return total


def _private_raw_audio_paths(bundle_dir: Path) -> list[Path]:
    paths: list[Path] = []
    for rel_path in RAW_AUDIO_RELATIVE_PATHS:
        path = bundle_dir / rel_path
        if path.is_file():
            paths.append(path)
    for dirname in RAW_AUDIO_DIRS:
        root = bundle_dir / dirname
        if root.is_dir():
            paths.extend(path for path in root.glob("*.wav") if path.is_file())
    return sorted(paths)


def _is_positive_int(value: object) -> bool:
    """Whether ``value`` is a JSON-style positive integer, excluding bool."""

    return type(value) is int and value > 0


def _legacy_schema_override(
    bundle_dir: Path,
    requested: int | None,
    *,
    info_payload: dict[str, Any] | None = None,
) -> int | None:
    """Own Room's schema-5 fallback without exposing it to the neutral core."""

    if requested is not None or info_payload is not None:
        return requested
    if (bundle_dir / "info.json").exists():
        return None
    return CURRENT_BUNDLE_SCHEMA_VERSION


def record_artifact(
    bundle_dir: Path,
    artifact_path: Path | str,
    *,
    kind: str,
    sensitivity: str,
    recomputable: bool,
    generated_by: str,
    bundle_schema_version: int | None = None,
    dependencies: Iterable[str] = (),
    schema_version: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record one bundle artifact in artifact_manifest.json.

    The manifest is an integrity surface, not a transaction log: entries
    are upserted by relative path, so frequently rewritten artifacts
    such as info.json keep one current checksum. The owning schema comes from
    info.json when present, and any explicit value must agree with it. Bundles
    without info.json use an explicit value or the current correction schema.

    Thread-safety: the per-file write is atomic (tempfile + os.replace),
    but the manifest read-modify-write below is NOT. It is safe today
    only because there is one global MeasurementSession and the state
    machine serializes analysis (ANALYZING is reset-busy), so just one
    worker touches a given bundle at a time — even now that analysis
    runs on an asyncio.to_thread worker. A future multi-session or
    parallel-bundle change MUST add a per-bundle lock here.
    """
    return _record_artifact(
        bundle_dir,
        artifact_path,
        kind=kind,
        sensitivity=sensitivity,
        recomputable=recomputable,
        generated_by=generated_by,
        bundle_schema_version=_legacy_schema_override(
            bundle_dir,
            bundle_schema_version,
        ),
        dependencies=dependencies,
        schema_version=schema_version,
        metadata=metadata,
    )


def write_json_artifact(
    bundle_dir: Path,
    relative_path: str,
    payload: dict[str, Any],
    *,
    kind: str,
    sensitivity: str,
    recomputable: bool,
    generated_by: str,
    bundle_schema_version: int | None = None,
    dependencies: Iterable[str] = (),
    schema_version: int | None = None,
    file_mode: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Atomically write JSON and update its manifest entry."""
    rel_path = _relative_artifact_path(bundle_dir, relative_path)
    _write_json_artifact(
        bundle_dir,
        relative_path,
        payload,
        kind=kind,
        sensitivity=sensitivity,
        recomputable=recomputable,
        generated_by=generated_by,
        bundle_schema_version=_legacy_schema_override(
            bundle_dir,
            bundle_schema_version,
            info_payload=payload if rel_path == "info.json" else None,
        ),
        dependencies=dependencies,
        schema_version=schema_version,
        file_mode=file_mode,
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
    info["has_acoustic_quality_json"] = (bundle_dir / "acoustic_quality.json").exists()
    info["has_mic_calibration_json"] = (bundle_dir / "mic_calibration.json").exists()
    info["has_mic_calibration_txt"] = (bundle_dir / "mic_calibration.txt").exists()
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
    raw_audio_paths = _private_raw_audio_paths(bundle_dir)
    raw_audio_bytes = 0
    for path in raw_audio_paths:
        try:
            raw_audio_bytes += path.stat().st_size
        except OSError:
            continue
    info["bundle_size_bytes"] = _bundle_byte_size(bundle_dir)
    info["private_raw_audio_count"] = len(raw_audio_paths)
    info["private_raw_audio_bytes"] = raw_audio_bytes
    return info


def _sorted_bundle_dirs(sessions_dir: Path) -> list[Path]:
    """Return parseable bundle directories newest-first."""
    if not sessions_dir.is_dir():
        return []
    candidates: list[tuple[float, str, Path]] = []
    for sub in sessions_dir.iterdir():
        if not sub.is_dir() or not (sub / "info.json").exists():
            continue
        try:
            info = _read_json(sub / "info.json")
        except BundleError:
            continue
        try:
            started_at = float(info.get("started_at") or 0)
        except (TypeError, ValueError):
            started_at = 0.0
        candidates.append((started_at, sub.name, sub))
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [bundle_dir for _, _, bundle_dir in candidates]


def list_bundles(
    sessions_dir: Path,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """List parseable bundles newest-first, skipping partial writes."""
    if limit <= 0:
        return []
    entries: list[dict[str, Any]] = []
    for bundle_dir in _sorted_bundle_dirs(sessions_dir)[:limit]:
        try:
            entries.append(summarize_bundle(bundle_dir))
        except BundleError:
            continue
    return entries


def summarize_bundle_collection(
    sessions_dir: Path,
    *,
    old_raw_audio_seconds: float = 7 * 24 * 60 * 60,
) -> dict[str, Any]:
    """Summarize all parseable correction bundles for operator visibility.

    This is intentionally a filesystem scan rather than a cached index:
    doctor is an explicit diagnostic command, and keeping the bundle
    directory as the source of truth avoids another state file that can
    drift from the evidence it reports on.
    """
    summaries: list[dict[str, Any]] = []
    for bundle_dir in _sorted_bundle_dirs(sessions_dir):
        try:
            summaries.append(summarize_bundle(bundle_dir))
        except BundleError:
            continue

    now = time.time()
    old_raw_audio_bundle_count = 0
    old_raw_audio_count = 0
    for summary in summaries:
        raw_count = int(summary.get("private_raw_audio_count") or 0)
        if raw_count <= 0:
            continue
        try:
            started_at = float(summary.get("started_at") or 0)
        except (TypeError, ValueError):
            started_at = 0.0
        if started_at > 0 and now - started_at >= old_raw_audio_seconds:
            old_raw_audio_bundle_count += 1
            old_raw_audio_count += raw_count

    return {
        "bundle_count": len(summaries),
        "latest_bundle": summaries[0] if summaries else None,
        "total_bundle_size_bytes": sum(
            int(summary.get("bundle_size_bytes") or 0) for summary in summaries
        ),
        "private_raw_audio_count": sum(
            int(summary.get("private_raw_audio_count") or 0) for summary in summaries
        ),
        "private_raw_audio_bytes": sum(
            int(summary.get("private_raw_audio_bytes") or 0) for summary in summaries
        ),
        "old_private_raw_audio_bundle_count": old_raw_audio_bundle_count,
        "old_private_raw_audio_count": old_raw_audio_count,
    }


def validate_bundle(
    bundle_dir: Path,
    *,
    max_sha_verify_bytes: int | None = DEFAULT_MAX_SHA_VERIFY_BYTES,
) -> list[BundleIssue]:
    """Validate the bundle contract enough for doctor/agent intake.

    max_sha_verify_bytes bounds the manifest SHA-256 re-verification:
    artifacts larger than it are checked by byte_size only (see
    DEFAULT_MAX_SHA_VERIFY_BYTES). Pass None to force a full hash check
    of every artifact (the forensic CLI path).
    """
    issues: list[BundleIssue] = []
    try:
        info = _read_json(bundle_dir / "info.json")
    except BundleError as e:
        return [BundleIssue("info_json", "fail", str(e))]

    schema = info.get("bundle_schema_version")
    if not _is_exact_version(schema, CURRENT_BUNDLE_SCHEMA_VERSION):
        issues.append(
            BundleIssue(
                "schema_version",
                "warn",
                f"bundle schema {schema!r}; expected {CURRENT_BUNDLE_SCHEMA_VERSION}",
            )
        )
    _validate_artifact_manifest(
        bundle_dir,
        issues,
        info_bundle_schema_version=schema,
        require_manifest=_is_exact_version(schema, CURRENT_BUNDLE_SCHEMA_VERSION),
        max_sha_verify_bytes=max_sha_verify_bytes,
    )
    state = info.get("state")
    if not info.get("session_id"):
        issues.append(
            BundleIssue(
                "session_id",
                "fail",
                "info.json missing session_id",
            )
        )
    if not state:
        issues.append(BundleIssue("state", "fail", "info.json missing state"))
    elif state == "failed":
        detail = info.get("error") or "no error recorded"
        issues.append(
            BundleIssue(
                "session_failed",
                "warn",
                f"bundle state=failed: {detail}",
            )
        )

    result_path = bundle_dir / "result.json"
    if state in {"ready", "applied", "verified"}:
        if not result_path.exists():
            issues.append(
                BundleIssue(
                    "result_json_missing",
                    "warn",
                    f"state={info.get('state')} but result.json is missing",
                )
            )
        else:
            try:
                result = _read_json(result_path)
                if not _is_exact_version(
                    result.get("bundle_schema_version"),
                    CURRENT_BUNDLE_SCHEMA_VERSION,
                ):
                    issues.append(
                        BundleIssue(
                            "result_schema_version",
                            "warn",
                            "result.json schema does not match current bundle schema",
                        )
                    )
            except BundleError as e:
                issues.append(BundleIssue("result_json", "fail", str(e)))

    mic = info.get("mic_calibration")
    if mic:
        if not (bundle_dir / "mic_calibration.json").exists():
            issues.append(
                BundleIssue(
                    "mic_calibration_json_missing",
                    "warn",
                    "mic_calibration metadata present but mic_calibration.json missing",
                )
            )
        if not (bundle_dir / "mic_calibration.txt").exists():
            issues.append(
                BundleIssue(
                    "mic_calibration_txt_missing",
                    "warn",
                    "mic_calibration metadata present but raw calibration file missing",
                )
            )

    runtime_path = bundle_dir / "runtime_integrity.json"
    runtime_summary = info.get("runtime_integrity")
    if runtime_summary and not runtime_path.exists():
        issues.append(
            BundleIssue(
                "runtime_integrity_json_missing",
                "warn",
                "runtime_integrity summary present but runtime_integrity.json missing",
            )
        )
    if runtime_path.exists():
        try:
            runtime = _read_json(runtime_path)
            if not _is_exact_version(runtime.get("artifact_schema_version"), 1):
                issues.append(
                    BundleIssue(
                        "runtime_integrity_schema_version",
                        "warn",
                        "runtime_integrity.json schema does not match current version",
                    )
                )
            for issue in runtime.get("issues") or []:
                if not isinstance(issue, dict):
                    continue
                severity = issue.get("severity")
                if severity in {"warn", "fail"}:
                    issues.append(
                        BundleIssue(
                            str(issue.get("code") or "runtime_integrity"),
                            severity,
                            str(issue.get("message") or "runtime integrity issue"),
                        )
                    )
        except BundleError as e:
            issues.append(BundleIssue("runtime_integrity_json", "fail", str(e)))

    acoustic_path = bundle_dir / "acoustic_quality.json"
    acoustic_summary = info.get("acoustic_quality")
    if acoustic_summary and not acoustic_path.exists():
        issues.append(
            BundleIssue(
                "acoustic_quality_json_missing",
                "warn",
                "acoustic_quality summary present but acoustic_quality.json missing",
            )
        )
    if acoustic_path.exists():
        try:
            acoustic = _read_json(acoustic_path)
            if not _is_exact_version(acoustic.get("artifact_schema_version"), 1):
                issues.append(
                    BundleIssue(
                        "acoustic_quality_schema_version",
                        "warn",
                        "acoustic_quality.json schema does not match current version",
                    )
                )
            for issue in acoustic.get("issues") or []:
                if not isinstance(issue, dict):
                    continue
                severity = issue.get("severity")
                if severity in {"warn", "fail"}:
                    issues.append(
                        BundleIssue(
                            str(issue.get("code") or "acoustic_quality"),
                            severity,
                            str(issue.get("message") or "acoustic quality issue"),
                        )
                    )
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
                issues.append(
                    BundleIssue(
                        str(issue.get("code") or "capture_quality"),
                        severity,
                        str(issue.get("message") or "capture quality issue"),
                    )
                )
    return issues


def _validate_artifact_manifest(
    bundle_dir: Path,
    issues: list[BundleIssue],
    *,
    info_bundle_schema_version: object | None,
    require_manifest: bool,
    max_sha_verify_bytes: int | None = DEFAULT_MAX_SHA_VERIFY_BYTES,
) -> None:
    manifest_path = _manifest_path(bundle_dir)
    if not manifest_path.exists():
        if require_manifest:
            issues.append(
                BundleIssue(
                    "artifact_manifest_missing",
                    "warn",
                    "current-schema bundle is missing artifact_manifest.json",
                )
            )
        return
    try:
        manifest = _read_json(manifest_path)
    except BundleError as e:
        issues.append(BundleIssue("artifact_manifest", "fail", str(e)))
        return

    if not _is_exact_version(
        manifest.get("manifest_schema_version"),
        CURRENT_ARTIFACT_MANIFEST_VERSION,
    ):
        issues.append(
            BundleIssue(
                "artifact_manifest_schema_version",
                "warn",
                "artifact_manifest.json schema does not match current version",
            )
        )

    manifest_bundle_schema_version = manifest.get("bundle_schema_version")
    if not _is_positive_int(manifest_bundle_schema_version):
        issues.append(
            BundleIssue(
                "artifact_manifest_bundle_schema_version",
                "fail",
                "artifact_manifest.json bundle_schema_version must be a positive integer",
            )
        )
    elif (
        _is_positive_int(info_bundle_schema_version)
        and manifest_bundle_schema_version != info_bundle_schema_version
    ):
        issues.append(
            BundleIssue(
                "artifact_manifest_bundle_schema_mismatch",
                "fail",
                "artifact_manifest.json bundle schema "
                f"{manifest_bundle_schema_version!r} does not match "
                f"info.json schema {info_bundle_schema_version!r}",
            )
        )

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        issues.append(
            BundleIssue(
                "artifact_manifest_artifacts",
                "fail",
                "artifact_manifest.json artifacts must be a list",
            )
        )
        return

    entries: dict[str, dict[str, Any]] = {}
    for idx, entry in enumerate(artifacts):
        if not isinstance(entry, dict):
            issues.append(
                BundleIssue(
                    "artifact_manifest_entry",
                    "fail",
                    f"artifact_manifest.json entry {idx} must be an object",
                )
            )
            continue
        raw_path = entry.get("path")
        if not isinstance(raw_path, str):
            issues.append(
                BundleIssue(
                    "artifact_manifest_entry_path",
                    "fail",
                    f"artifact_manifest.json entry {idx} missing path",
                )
            )
            continue
        try:
            rel_path = _relative_artifact_path(bundle_dir, raw_path)
        except BundleError as e:
            issues.append(
                BundleIssue(
                    "artifact_manifest_entry_path",
                    "fail",
                    str(e),
                )
            )
            continue
        if rel_path in entries:
            issues.append(
                BundleIssue(
                    "artifact_manifest_duplicate_path",
                    "warn",
                    f"artifact_manifest.json repeats {rel_path}",
                )
            )
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
                issues.append(
                    BundleIssue(
                        "artifact_manifest_entry_field",
                        "warn",
                        f"{rel_path} missing manifest field {field}",
                    )
                )

        path = bundle_dir / rel_path
        if not path.exists():
            issues.append(
                BundleIssue(
                    "artifact_missing",
                    "fail",
                    f"manifest lists {rel_path}, but the file is missing",
                )
            )
            continue
        actual_size = path.stat().st_size
        expected_size = entry.get("byte_size")
        if isinstance(expected_size, int) and expected_size != actual_size:
            issues.append(
                BundleIssue(
                    "artifact_size_mismatch",
                    "fail",
                    f"{rel_path} byte_size changed since manifest record",
                )
            )
        expected_sha = entry.get("sha256")
        # Skip the expensive re-hash for large artifacts on the capped
        # (doctor) path; the byte_size check above is the retained
        # integrity gate. None = forensic path → always hash. The
        # short-circuit means oversized artifacts are never read.
        verify_sha = max_sha_verify_bytes is None or actual_size <= max_sha_verify_bytes
        if (
            isinstance(expected_sha, str)
            and verify_sha
            and expected_sha != _sha256_file(path)
        ):
            issues.append(
                BundleIssue(
                    "artifact_sha256_mismatch",
                    "fail",
                    f"{rel_path} sha256 changed since manifest record",
                )
            )

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
            issues.append(
                BundleIssue(
                    "artifact_dependencies",
                    "warn",
                    f"{rel_path} dependencies must be a list",
                )
            )
            continue
        for dep in dependencies:
            if not isinstance(dep, str):
                issues.append(
                    BundleIssue(
                        "artifact_dependency",
                        "warn",
                        f"{rel_path} has a non-string dependency",
                    )
                )
                continue
            if dep not in entries:
                issues.append(
                    BundleIssue(
                        "artifact_dependency_missing",
                        "warn",
                        f"{rel_path} depends on {dep}, which is not in the manifest",
                    )
                )


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
            issues.append(
                BundleIssue(
                    "artifact_manifest_missing_entry",
                    "warn",
                    f"{rel_path} exists but is not listed in artifact_manifest.json",
                )
            )


def latest_bundle(sessions_dir: Path) -> dict[str, Any] | None:
    bundles = list_bundles(sessions_dir, limit=1)
    return bundles[0] if bundles else None
