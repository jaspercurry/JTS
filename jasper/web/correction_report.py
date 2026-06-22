# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Read-only correction measurement-report payload helpers.

Keep this module import-cheap: the correction wizard web process is a
stdlib HTTP server, and the expensive DSP/scientific imports are delayed
until a report is actually requested.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class InvalidSessionId(ValueError):
    """Raised when a browser-provided session id is not a bundle id."""


def _read_optional_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def bundle_report_versions(bundle_dir: Path) -> dict[str, Any]:
    """Return expected and observed schema versions for report artifacts."""
    from jasper.correction import (
        acoustic_quality,
        bundles,
        evidence,
        runtime_integrity,
    )

    info = _read_optional_json(bundle_dir / "info.json") or {}
    result = _read_optional_json(bundle_dir / "result.json") or {}
    runtime = _read_optional_json(bundle_dir / "runtime_integrity.json") or {}
    acoustic = _read_optional_json(bundle_dir / "acoustic_quality.json") or {}
    manifest = _read_optional_json(bundle_dir / bundles.ARTIFACT_MANIFEST_NAME) or {}
    return {
        "expected_bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
        "expected_artifact_manifest_schema_version": (
            bundles.CURRENT_ARTIFACT_MANIFEST_VERSION
        ),
        "expected_runtime_integrity_schema_version": runtime_integrity.SCHEMA_VERSION,
        "expected_acoustic_quality_schema_version": acoustic_quality.SCHEMA_VERSION,
        "expected_evidence_packet_schema_version": evidence.SCHEMA_VERSION,
        "bundle_schema_version": info.get("bundle_schema_version"),
        "artifact_manifest_schema_version": manifest.get("manifest_schema_version"),
        "artifact_manifest_bundle_schema_version": (
            manifest.get("bundle_schema_version")
        ),
        "result_json_schema_version": result.get("bundle_schema_version"),
        "runtime_integrity_schema_version": runtime.get("artifact_schema_version"),
        "acoustic_quality_schema_version": acoustic.get("artifact_schema_version"),
        "evidence_packet_schema_version": evidence.SCHEMA_VERSION,
    }


def resolve_session_bundle_dir(sessions_dir: Path, session_id: str) -> Path:
    """Resolve a browser-provided session id to a bundle under sessions_dir."""
    clean = session_id.strip()
    if not clean:
        raise InvalidSessionId("missing session id")
    if len(clean) > 128:
        raise InvalidSessionId("session id is too long")
    root = sessions_dir.resolve()
    candidate = sessions_dir / clean
    try:
        bundle_dir = candidate.resolve(strict=False)
        bundle_dir.relative_to(root)
    except (OSError, ValueError) as e:
        raise InvalidSessionId("invalid session id") from e
    if not bundle_dir.is_dir():
        raise FileNotFoundError(f"session bundle not found: {clean}")
    return bundle_dir


def build_session_report_payload(
    *,
    sessions_dir: Path,
    session_id: str,
) -> dict[str, Any]:
    """Build the browser-safe read-only report payload for one bundle."""
    from jasper.correction import bundles, evidence

    bundle_dir = resolve_session_bundle_dir(sessions_dir, session_id)
    packet = evidence.build_evidence_packet(bundle_dir)
    return {
        "session_id": packet.get("session_id") or bundle_dir.name,
        "summary": bundles.summarize_bundle(bundle_dir),
        "artifact_versions": bundle_report_versions(bundle_dir),
        "evidence": packet,
    }
