# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Neutral artifact-manifest primitives for acoustic evidence bundles.

This module owns only the byte-level manifest contract shared by room
correction and active-speaker commissioning.  Feature packages continue to
own their bundle schema, directory, retention, validation, and authority
rules.  A bundle is forensic evidence; reading it never grants playback or
apply authority.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jasper.log_event import log_event

CURRENT_ARTIFACT_MANIFEST_VERSION = 1
ARTIFACT_MANIFEST_NAME = "artifact_manifest.json"

logger = logging.getLogger(__name__)


class BundleError(RuntimeError):
    """An evidence bundle or artifact manifest is missing or malformed."""


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
    except OSError as exc:
        raise BundleError(f"could not read {path.name}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise BundleError(f"{path.name} is invalid JSON: {exc.msg}") from exc
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


def sha256_file(path: Path) -> str:
    """Return the content identity of one artifact file."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_artifact_path(bundle_dir: Path, artifact_path: Path | str) -> str:
    bundle_root = bundle_dir.resolve()
    path = Path(artifact_path)
    if not path.is_absolute():
        path = bundle_dir / path
    try:
        rel = path.resolve().relative_to(bundle_root)
    except ValueError as exc:
        raise BundleError(
            f"artifact path {artifact_path!s} is outside bundle"
        ) from exc
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
    for dependency in dependencies:
        try:
            out.append(_relative_artifact_path(bundle_dir, dependency))
        except BundleError:
            log_event(
                logger,
                # Compatibility vocabulary: these event names predate the
                # neutral module and are already used in journal greps.
                "correction_bundle_dependency_ignored",
                path=dependency,
                level=logging.WARNING,
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
    return [artifact for artifact in artifacts if isinstance(artifact, dict)]


def read_artifact_manifest(bundle_dir: Path) -> dict[str, Any]:
    """Read ``artifact_manifest.json`` for callers needing raw details."""

    return _read_json(_manifest_path(bundle_dir))


def _is_positive_int(value: object) -> bool:
    """Whether ``value`` is a JSON-style positive integer, excluding bool."""

    return type(value) is int and value > 0


def _is_exact_version(value: object, expected: int) -> bool:
    """Whether ``value`` is the exact positive-integer version expected."""

    return _is_positive_int(value) and value == expected


def _resolve_bundle_schema_version(
    bundle_dir: Path,
    explicit: int | None,
    *,
    info_payload: dict[str, Any] | None = None,
) -> int:
    """Resolve the feature-owned bundle schema from ``info.json``."""

    info = info_payload
    info_path = bundle_dir / "info.json"
    if info is None and info_path.exists():
        info = _read_json(info_path)

    if explicit is not None and not _is_positive_int(explicit):
        raise BundleError("bundle schema override must be a positive integer")

    if info is not None:
        if "bundle_schema_version" not in info:
            raise BundleError("info.json missing bundle_schema_version")
        owner_schema = info["bundle_schema_version"]
        if not _is_positive_int(owner_schema):
            raise BundleError(
                "info.json bundle_schema_version must be a positive integer"
            )
        if explicit is not None and explicit != owner_schema:
            raise BundleError(
                "bundle schema override "
                f"{explicit} contradicts info.json bundle_schema_version "
                f"{owner_schema}"
            )
        return owner_schema
    if explicit is not None:
        return explicit
    raise BundleError("bundle schema version is required without info.json")


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
    """Record one artifact in the owning feature's manifest.

    Entries are upserted by normalized relative path.  The manifest is an
    integrity surface, not a source of runtime authority.  Per-file writes are
    atomic; callers must continue to serialize writes to one bundle.  The
    feature schema must come from authoritative ``info.json`` or the explicit
    ``bundle_schema_version`` argument.
    """

    bundle_dir.mkdir(parents=True, exist_ok=True)
    rel_path = _relative_artifact_path(bundle_dir, artifact_path)
    path = bundle_dir / rel_path
    try:
        stat = path.stat()
    except OSError as exc:
        raise BundleError(f"artifact {rel_path} cannot be stat'ed: {exc}") from exc
    resolved_bundle_schema_version = _resolve_bundle_schema_version(
        bundle_dir,
        bundle_schema_version,
    )

    try:
        artifacts = _read_manifest_artifacts(bundle_dir)
    except BundleError as exc:
        log_event(
            logger,
            "correction_bundle_manifest_reset",
            bundle=bundle_dir,
            error=exc,
            level=logging.WARNING,
        )
        artifacts = []

    entry = ArtifactEntry(
        path=rel_path,
        kind=kind,
        sensitivity=sensitivity,
        recomputable=bool(recomputable),
        sha256=sha256_file(path),
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
            log_event(
                logger,
                "correction_bundle_manifest_entry_dropped",
                path=raw_path,
                level=logging.WARNING,
            )
            continue
        by_path[existing_rel_path] = {**artifact, "path": existing_rel_path}
    by_path[rel_path] = entry
    manifest = {
        "manifest_schema_version": CURRENT_ARTIFACT_MANIFEST_VERSION,
        "bundle_schema_version": resolved_bundle_schema_version,
        "generated_at": time.time(),
        "artifacts": [by_path[path] for path in sorted(by_path)],
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
    bundle_schema_version: int | None = None,
    dependencies: Iterable[str] = (),
    schema_version: int | None = None,
    file_mode: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Atomically write JSON and update its manifest entry."""

    rel_path = _relative_artifact_path(bundle_dir, relative_path)
    resolved_bundle_schema_version = _resolve_bundle_schema_version(
        bundle_dir,
        bundle_schema_version,
        info_payload=payload if rel_path == "info.json" else None,
    )
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
        bundle_schema_version=resolved_bundle_schema_version,
        dependencies=dependencies,
        schema_version=schema_version,
        metadata=metadata,
    )
