from __future__ import annotations

import json
from pathlib import Path

from jasper.correction import bundles


def _write_bundle(root: Path, name: str, *, started_at: int) -> Path:
    d = root / name
    d.mkdir()
    (d / "info.json").write_text(json.dumps({
        "bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
        "session_id": name,
        "state": "ready",
        "started_at": started_at,
        "capture_quality": [],
    }))
    (d / "result.json").write_text(json.dumps({
        "bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
    }))
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
