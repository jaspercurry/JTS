# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jasper.correction import bundles

from .correction_bundle_fixtures import write_golden_correction_bundle


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


def _write_info_bundle(bundle_dir: Path, *, schema_version: object) -> None:
    bundle_dir.mkdir()
    bundles.write_json_artifact(
        bundle_dir,
        "info.json",
        {"bundle_schema_version": schema_version},
        kind="metadata",
        sensitivity="config",
        recomputable=False,
        generated_by="test",
    )


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


def test_list_bundles_limits_before_expensive_summary(
    tmp_path: Path,
    monkeypatch,
):
    _write_bundle(tmp_path, "old", started_at=1000)
    _write_bundle(tmp_path, "mid", started_at=2000)
    _write_bundle(tmp_path, "new", started_at=3000)
    summarized: list[str] = []
    real_summarize = bundles.summarize_bundle

    def fake_summarize(bundle_dir: Path) -> dict:
        summarized.append(bundle_dir.name)
        return real_summarize(bundle_dir)

    monkeypatch.setattr(bundles, "summarize_bundle", fake_summarize)

    found = bundles.list_bundles(tmp_path, limit=1)

    assert [b["session_id"] for b in found] == ["new"]
    assert summarized == ["new"]


def test_summarize_bundle_reports_size_and_private_raw_audio(tmp_path: Path):
    bundle = _write_bundle(tmp_path, "with-audio", started_at=1000)
    capture_dir = bundle / "captures"
    noise_dir = bundle / "noise"
    repeat_dir = bundle / "repeat_captures"
    capture_dir.mkdir()
    noise_dir.mkdir()
    repeat_dir.mkdir()
    (capture_dir / "p0.wav").write_bytes(b"capture")
    (noise_dir / "p0_pre.wav").write_bytes(b"noise")
    (repeat_dir / "p0_r1.wav").write_bytes(b"repeat")
    (bundle / "verify.wav").write_bytes(b"verify")
    (bundle / "analysis").mkdir()
    (bundle / "analysis" / "p0_ir.wav").write_bytes(b"derived")

    summary = bundles.summarize_bundle(bundle)

    assert summary["private_raw_audio_count"] == 4
    assert summary["private_raw_audio_bytes"] == (
        len(b"capture") + len(b"noise") + len(b"repeat") + len(b"verify")
    )
    assert summary["bundle_size_bytes"] >= summary["private_raw_audio_bytes"]


def test_summarize_bundle_collection_reports_storage_and_private_audio(
    tmp_path: Path,
):
    write_golden_correction_bundle(tmp_path, "old", started_at=1000)
    write_golden_correction_bundle(tmp_path, "new", started_at=2000)

    summary = bundles.summarize_bundle_collection(
        tmp_path,
        old_raw_audio_seconds=0,
    )

    assert summary["bundle_count"] == 2
    assert summary["latest_bundle"]["session_id"] == "new"
    assert summary["total_bundle_size_bytes"] > 0
    assert summary["private_raw_audio_count"] == 8
    assert summary["private_raw_audio_bytes"] > 0
    assert summary["old_private_raw_audio_bundle_count"] == 2
    assert summary["old_private_raw_audio_count"] == 8


def test_golden_correction_bundle_fixture_validates_contract(tmp_path: Path):
    bundle = write_golden_correction_bundle(tmp_path)

    issues = bundles.validate_bundle(bundle)
    summary = bundles.summarize_bundle(bundle)

    assert issues == []
    assert summary["has_artifact_manifest"] is True
    assert summary["has_runtime_integrity_json"] is True
    assert summary["has_acoustic_quality_json"] is True
    assert summary["private_raw_audio_count"] == 4
    assert (bundle / "evidence_packet.json").exists()


def test_list_bundles_treats_missing_or_file_sessions_dir_as_empty(
    tmp_path: Path,
):
    assert bundles.list_bundles(tmp_path / "missing") == []
    assert bundles.summarize_bundle_collection(tmp_path / "missing") == {
        "bundle_count": 0,
        "latest_bundle": None,
        "total_bundle_size_bytes": 0,
        "private_raw_audio_count": 0,
        "private_raw_audio_bytes": 0,
        "old_private_raw_audio_bundle_count": 0,
        "old_private_raw_audio_count": 0,
    }
    not_dir = tmp_path / "sessions"
    not_dir.write_text("not a directory")
    assert bundles.list_bundles(not_dir) == []


def test_validate_bundle_reports_missing_result_and_quality_warnings(
    tmp_path: Path,
):
    d = tmp_path / "aaa"
    d.mkdir()
    (d / "info.json").write_text(
        json.dumps(
            {
                "bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
                "session_id": "aaa",
                "state": "ready",
                "capture_quality": [
                    {
                        "issues": [
                            {
                                "code": "mic_uncalibrated",
                                "severity": "warn",
                                "message": "no measurement-mic calibration was applied",
                            }
                        ],
                    }
                ],
                "verify_quality": {
                    "issues": [
                        {
                            "code": "capture_rms_low",
                            "severity": "warn",
                            "message": "capture RMS is very low",
                        }
                    ],
                },
            }
        )
    )

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


@pytest.mark.parametrize(
    "schema_version",
    [None, True, "5", 5.0, 0, -1],
)
def test_validate_bundle_reports_one_noncurrent_info_schema_warning(
    tmp_path: Path,
    schema_version: object,
):
    d = tmp_path / "invalid-info-version"
    d.mkdir()
    info = {
        "session_id": "invalid-info-version",
        "state": "open",
    }
    if schema_version is not None:
        info["bundle_schema_version"] = schema_version
    (d / "info.json").write_text(json.dumps(info))

    issues = bundles.validate_bundle(d)

    assert [(issue.code, issue.severity) for issue in issues] == [
        ("schema_version", "warn")
    ]


@pytest.mark.parametrize(
    "schema_version",
    [None, True, "5", 5.0, 0, -1],
)
def test_validate_bundle_reports_one_noncurrent_result_schema_warning(
    tmp_path: Path,
    schema_version: object,
):
    d = _write_bundle(tmp_path, "invalid-result-version", started_at=1000)
    result = {}
    if schema_version is not None:
        result["bundle_schema_version"] = schema_version
    bundles.write_json_artifact(
        d,
        "result.json",
        result,
        kind="analysis_result",
        sensitivity="private_metadata",
        recomputable=True,
        generated_by="test",
        dependencies=["info.json"],
        schema_version=bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
    )

    issues = bundles.validate_bundle(d)

    assert [(issue.code, issue.severity) for issue in issues] == [
        ("result_schema_version", "warn")
    ]


def test_record_artifact_writes_manifest_with_checksum(tmp_path: Path):
    d = tmp_path / "aaa"
    d.mkdir()
    (d / "result.json").write_text("{}")

    entry = bundles.record_artifact(
        d,
        "result.json",
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
    assert manifest["bundle_schema_version"] == (bundles.CURRENT_BUNDLE_SCHEMA_VERSION)
    assert manifest["artifacts"] == [entry]
    assert entry["path"] == "result.json"
    assert entry["byte_size"] == (d / "result.json").stat().st_size
    assert len(entry["sha256"]) == 64


def test_record_artifact_keeps_info_bundle_schema_without_override(tmp_path: Path):
    d = tmp_path / "active-speaker"
    _write_info_bundle(d, schema_version=1)
    (d / "capture.wav").write_bytes(b"capture")

    bundles.record_artifact(
        d,
        "capture.wav",
        kind="capture_wav",
        sensitivity="private_raw_audio",
        recomputable=False,
        generated_by="test",
    )

    manifest = bundles.read_artifact_manifest(d)
    assert manifest["bundle_schema_version"] == 1
    assert {entry["path"] for entry in manifest["artifacts"]} == {
        "capture.wav",
        "info.json",
    }


def test_record_artifact_repairs_manifest_bundle_schema_from_info(tmp_path: Path):
    d = tmp_path / "active-speaker"
    _write_info_bundle(d, schema_version=1)
    stale_manifest = bundles.read_artifact_manifest(d)
    old_entry = next(
        entry for entry in stale_manifest["artifacts"] if entry["path"] == "info.json"
    )
    stale_manifest["bundle_schema_version"] = bundles.CURRENT_BUNDLE_SCHEMA_VERSION
    (d / bundles.ARTIFACT_MANIFEST_NAME).write_text(json.dumps(stale_manifest))
    (d / "next.json").write_text("{}")

    bundles.record_artifact(
        d,
        "next.json",
        kind="derived",
        sensitivity="debug_safe",
        recomputable=True,
        generated_by="test",
    )

    repaired = bundles.read_artifact_manifest(d)
    assert repaired["bundle_schema_version"] == 1
    by_path = {entry["path"]: entry for entry in repaired["artifacts"]}
    assert by_path["info.json"] == old_entry
    assert by_path["info.json"]["sha256"] == old_entry["sha256"]
    assert set(by_path) == {"info.json", "next.json"}


@pytest.mark.parametrize("schema_version", [True, "1", 1.0, 0, -1])
def test_info_bundle_schema_requires_a_positive_integer(
    tmp_path: Path,
    schema_version: object,
):
    d = tmp_path / "invalid-info-schema"

    with pytest.raises(bundles.BundleError, match="positive integer"):
        _write_info_bundle(d, schema_version=schema_version)

    assert not (d / "info.json").exists()
    assert not (d / bundles.ARTIFACT_MANIFEST_NAME).exists()


@pytest.mark.parametrize("schema_version", [True, "1", 1.0, 0, -1])
def test_explicit_bundle_schema_requires_a_positive_integer(
    tmp_path: Path,
    schema_version: object,
):
    d = tmp_path / "invalid-explicit-schema"
    d.mkdir()
    artifact = d / "result.json"
    artifact.write_text("{}")

    with pytest.raises(bundles.BundleError, match="positive integer"):
        bundles.record_artifact(
            d,
            artifact,
            kind="derived",
            sensitivity="debug_safe",
            recomputable=True,
            generated_by="test",
            bundle_schema_version=schema_version,  # type: ignore[arg-type]
        )

    assert not (d / bundles.ARTIFACT_MANIFEST_NAME).exists()


def test_write_json_artifact_refuses_schema_override_conflicting_with_info(
    tmp_path: Path,
):
    d = tmp_path / "active-speaker"
    _write_info_bundle(d, schema_version=1)
    manifest_before = (d / bundles.ARTIFACT_MANIFEST_NAME).read_text()

    with pytest.raises(bundles.BundleError, match="contradicts info.json"):
        bundles.write_json_artifact(
            d,
            "result.json",
            {},
            kind="derived",
            sensitivity="debug_safe",
            recomputable=True,
            generated_by="test",
            bundle_schema_version=bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
        )

    assert not (d / "result.json").exists()
    assert (d / bundles.ARTIFACT_MANIFEST_NAME).read_text() == manifest_before


@pytest.mark.parametrize("schema_version", [True, 1.0])
def test_schema_override_equality_traps_do_not_match_info_owner(
    tmp_path: Path,
    schema_version: object,
):
    d = tmp_path / "active-speaker"
    _write_info_bundle(d, schema_version=1)
    artifact = d / "result.json"
    artifact.write_text("{}")

    with pytest.raises(bundles.BundleError, match="positive integer"):
        bundles.record_artifact(
            d,
            artifact,
            kind="derived",
            sensitivity="debug_safe",
            recomputable=True,
            generated_by="test",
            bundle_schema_version=schema_version,  # type: ignore[arg-type]
        )

    manifest = bundles.read_artifact_manifest(d)
    assert manifest["bundle_schema_version"] == 1
    assert {entry["path"] for entry in manifest["artifacts"]} == {"info.json"}


def test_validate_bundle_reports_manifest_info_schema_mismatch(tmp_path: Path):
    d = _write_bundle(tmp_path, "aaa", started_at=1000)
    manifest = bundles.read_artifact_manifest(d)
    manifest["bundle_schema_version"] = 1
    (d / bundles.ARTIFACT_MANIFEST_NAME).write_text(json.dumps(manifest))

    issues = bundles.validate_bundle(d)

    matching = [
        issue
        for issue in issues
        if issue.code == "artifact_manifest_bundle_schema_mismatch"
    ]
    assert [(issue.code, issue.severity) for issue in matching] == [
        ("artifact_manifest_bundle_schema_mismatch", "fail")
    ]


@pytest.mark.parametrize(
    "schema_version",
    [None, True, "1", 1.0, 0, -1],
)
def test_validate_bundle_reports_one_noncurrent_manifest_schema_warning(
    tmp_path: Path,
    schema_version: object,
):
    d = _write_bundle(tmp_path, "aaa", started_at=1000)
    manifest = bundles.read_artifact_manifest(d)
    if schema_version is None:
        manifest.pop("manifest_schema_version")
    else:
        manifest["manifest_schema_version"] = schema_version
    (d / bundles.ARTIFACT_MANIFEST_NAME).write_text(json.dumps(manifest))

    issues = bundles.validate_bundle(d)

    assert [(issue.code, issue.severity) for issue in issues] == [
        ("artifact_manifest_schema_version", "warn")
    ]


@pytest.mark.parametrize("schema_version", [None, True, "5", 5.0, 0, -1])
def test_validate_bundle_reports_one_malformed_manifest_bundle_schema_issue(
    tmp_path: Path,
    schema_version: object,
):
    d = _write_bundle(tmp_path, "aaa", started_at=1000)
    manifest = bundles.read_artifact_manifest(d)
    if schema_version is None:
        manifest.pop("bundle_schema_version")
    else:
        manifest["bundle_schema_version"] = schema_version
    (d / bundles.ARTIFACT_MANIFEST_NAME).write_text(json.dumps(manifest))

    issues = bundles.validate_bundle(d)

    assert [(issue.code, issue.severity) for issue in issues] == [
        ("artifact_manifest_bundle_schema_version", "fail")
    ]


@pytest.mark.parametrize("schema_version", [True, 1.0])
def test_validate_bundle_does_not_accept_manifest_schema_equality_traps(
    tmp_path: Path,
    schema_version: object,
):
    d = tmp_path / "schema-one"
    d.mkdir()
    (d / "info.json").write_text(
        json.dumps(
            {
                "bundle_schema_version": 1,
                "session_id": "schema-one",
                "state": "open",
            }
        )
    )
    (d / bundles.ARTIFACT_MANIFEST_NAME).write_text(
        json.dumps(
            {
                "manifest_schema_version": bundles.CURRENT_ARTIFACT_MANIFEST_VERSION,
                "bundle_schema_version": schema_version,
                "artifacts": [],
            }
        )
    )

    issues = bundles.validate_bundle(d)

    matching = [
        issue
        for issue in issues
        if issue.code.startswith("artifact_manifest_bundle_schema")
    ]
    assert [(issue.code, issue.severity) for issue in matching] == [
        ("artifact_manifest_bundle_schema_version", "fail")
    ]


@pytest.mark.parametrize(
    ("artifact_name", "issue_code"),
    (
        ("runtime_integrity.json", "runtime_integrity_schema_version"),
        ("acoustic_quality.json", "acoustic_quality_schema_version"),
    ),
)
@pytest.mark.parametrize("schema_version", [None, True, "1", 1.0, 0, -1])
def test_validate_bundle_rejects_artifact_schema_equality_traps(
    tmp_path: Path,
    artifact_name: str,
    issue_code: str,
    schema_version: object,
):
    d = _write_bundle(tmp_path, "invalid-artifact-version", started_at=1000)
    artifact = {}
    if schema_version is not None:
        artifact["artifact_schema_version"] = schema_version
    bundles.write_json_artifact(
        d,
        artifact_name,
        artifact,
        kind="derived",
        sensitivity="private_metadata",
        recomputable=True,
        generated_by="test",
        dependencies=["info.json"],
        schema_version=1,
    )

    issues = bundles.validate_bundle(d)

    assert [(issue.code, issue.severity) for issue in issues] == [(issue_code, "warn")]


def test_validate_bundle_reports_manifest_checksum_mismatch(tmp_path: Path):
    d = _write_bundle(tmp_path, "aaa", started_at=1000)
    (d / "result.json").write_text('{"tampered": true}')

    issues = bundles.validate_bundle(d)

    assert ("artifact_sha256_mismatch", "fail") in {
        (issue.code, issue.severity) for issue in issues
    }


def test_validate_bundle_caps_sha_for_large_artifacts(tmp_path: Path):
    """The frequent doctor path skips the full SHA re-hash for large
    raw-audio artifacts — byte_size stays the integrity gate — so it
    doesn't re-hash every WAV on every run. The forensic path
    (max_sha_verify_bytes=None) still catches a same-size content
    tamper."""
    d = _write_bundle(tmp_path, "big", started_at=1000)
    (d / "captures").mkdir()
    wav = d / "captures" / "p0.wav"
    payload = b"\x00" * (bundles.DEFAULT_MAX_SHA_VERIFY_BYTES + 1024)
    wav.write_bytes(payload)
    bundles.record_artifact(
        d,
        "captures/p0.wav",
        kind="raw_capture",
        sensitivity="private_raw_audio",
        recomputable=False,
        generated_by="tests.test_correction_bundles",
        schema_version=bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
    )
    # Tamper content but keep byte_size identical: only a SHA check sees it.
    wav.write_bytes(b"\xff" + payload[1:])
    assert wav.stat().st_size == len(payload)

    capped = bundles.validate_bundle(d)
    assert not any(i.code == "artifact_sha256_mismatch" for i in capped)
    assert not any(i.code == "artifact_size_mismatch" for i in capped)

    full = bundles.validate_bundle(d, max_sha_verify_bytes=None)
    assert ("artifact_sha256_mismatch", "fail") in {
        (issue.code, issue.severity) for issue in full
    }


def test_validate_bundle_warns_when_current_bundle_missing_manifest(
    tmp_path: Path,
):
    d = tmp_path / "aaa"
    d.mkdir()
    (d / "info.json").write_text(
        json.dumps(
            {
                "bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
                "session_id": "aaa",
                "state": "failed",
                "capture_quality": [],
            }
        )
    )

    issues = bundles.validate_bundle(d)

    assert ("artifact_manifest_missing", "warn") in {
        (issue.code, issue.severity) for issue in issues
    }


def test_validate_bundle_warns_when_latest_session_failed(tmp_path: Path):
    d = tmp_path / "failed"
    d.mkdir()
    (d / "info.json").write_text(
        json.dumps(
            {
                "bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
                "session_id": "failed",
                "state": "failed",
                "error": "analysis failed: capture clipped",
                "capture_quality": [],
            }
        )
    )

    issues = bundles.validate_bundle(d)

    assert any(
        issue.code == "session_failed"
        and issue.severity == "warn"
        and "capture clipped" in issue.message
        for issue in issues
    )
