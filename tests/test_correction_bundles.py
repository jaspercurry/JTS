from __future__ import annotations

import json
from pathlib import Path

from jasper.correction import bundles


def _write_bundle(root: Path, name: str, *, started_at: int) -> Path:
    d = root / name
    d.mkdir()
    bundles.write_json_artifact(
        d,
        "info.json",
        {
            "bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
            "session_id": name,
            "state": "ready",
            "started_at": started_at,
            "capture_quality": [],
        },
        kind="session_metadata",
        sensitivity="private_metadata",
        recomputable=False,
        generated_by="tests.test_correction_bundles._write_bundle",
        schema_version=bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
    )
    bundles.write_json_artifact(
        d,
        "result.json",
        {"bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION},
        kind="analysis_result",
        sensitivity="private_metadata",
        recomputable=True,
        generated_by="tests.test_correction_bundles._write_bundle",
        dependencies=["info.json"],
        schema_version=bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
    )
    return d


def test_list_bundles_sorts_newest_first_and_skips_bad_json(tmp_path: Path):
    _write_bundle(tmp_path, "old", started_at=1000)
    _write_bundle(tmp_path, "new", started_at=2000)
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "info.json").write_text("not json")

    found = bundles.list_bundles(tmp_path)

    assert [b["session_id"] for b in found] == ["new", "old"]
    assert found[0]["has_result"] is True
    assert found[0]["has_mic_calibration_json"] is False
    assert found[0]["has_artifact_manifest"] is True
    assert found[0]["artifact_count"] == 2


def test_list_bundles_treats_missing_or_file_sessions_dir_as_empty(
    tmp_path: Path,
):
    assert bundles.list_bundles(tmp_path / "missing") == []
    not_dir = tmp_path / "sessions"
    not_dir.write_text("not a directory")
    assert bundles.list_bundles(not_dir) == []


def test_validate_bundle_reports_missing_result_and_quality_warnings(
    tmp_path: Path,
):
    d = tmp_path / "aaa"
    d.mkdir()
    (d / "info.json").write_text(json.dumps({
        "bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
        "session_id": "aaa",
        "state": "ready",
        "capture_quality": [{
            "issues": [{
                "code": "mic_uncalibrated",
                "severity": "warn",
                "message": "no measurement-mic calibration was applied",
            }],
        }],
        "verify_quality": {
            "issues": [{
                "code": "capture_rms_low",
                "severity": "warn",
                "message": "capture RMS is very low",
            }],
        },
    }))

    issues = bundles.validate_bundle(d)

    assert ("result_json_missing", "warn") in {
        (issue.code, issue.severity) for issue in issues
    }
    assert ("mic_uncalibrated", "warn") in {
        (issue.code, issue.severity) for issue in issues
    }
    assert ("capture_rms_low", "warn") in {
        (issue.code, issue.severity) for issue in issues
    }


def test_record_artifact_writes_manifest_with_checksum(tmp_path: Path):
    d = tmp_path / "aaa"
    d.mkdir()
    (d / "info.json").write_text("{}")

    entry = bundles.record_artifact(
        d,
        "info.json",
        kind="session_metadata",
        sensitivity="private_metadata",
        recomputable=False,
        generated_by="test",
        schema_version=bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
    )

    manifest = bundles.read_artifact_manifest(d)
    assert manifest["manifest_schema_version"] == (
        bundles.CURRENT_ARTIFACT_MANIFEST_VERSION
    )
    assert manifest["artifacts"] == [entry]
    assert entry["path"] == "info.json"
    assert entry["byte_size"] == (d / "info.json").stat().st_size
    assert len(entry["sha256"]) == 64


def test_validate_bundle_reports_manifest_checksum_mismatch(tmp_path: Path):
    d = _write_bundle(tmp_path, "aaa", started_at=1000)
    (d / "result.json").write_text('{"tampered": true}')

    issues = bundles.validate_bundle(d)

    assert ("artifact_sha256_mismatch", "fail") in {
        (issue.code, issue.severity) for issue in issues
    }


def test_validate_bundle_warns_when_current_bundle_missing_manifest(
    tmp_path: Path,
):
    d = tmp_path / "aaa"
    d.mkdir()
    (d / "info.json").write_text(json.dumps({
        "bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
        "session_id": "aaa",
        "state": "failed",
        "capture_quality": [],
    }))

    issues = bundles.validate_bundle(d)

    assert ("artifact_manifest_missing", "warn") in {
        (issue.code, issue.severity) for issue in issues
    }


def test_validate_bundle_warns_when_latest_session_failed(tmp_path: Path):
    d = tmp_path / "failed"
    d.mkdir()
    (d / "info.json").write_text(json.dumps({
        "bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
        "session_id": "failed",
        "state": "failed",
        "error": "analysis failed: capture clipped",
        "capture_quality": [],
    }))

    issues = bundles.validate_bundle(d)

    assert any(
        issue.code == "session_failed"
        and issue.severity == "warn"
        and "capture clipped" in issue.message
        for issue in issues
    )
