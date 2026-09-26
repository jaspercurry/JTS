# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import math
import stat
from datetime import timedelta

import pytest

from jasper import audio_validation, audio_validation_artifacts as artifacts
from jasper.audio_validation_artifacts import (
    ArtifactLoadResult,
    ValidationArtifact,
    ValidationArtifactError,
    artifact_age,
    is_artifact_from_future,
    is_artifact_stale,
    load_artifact,
    load_latest_artifact,
    make_artifact,
    parse_artifact_payload,
    write_artifact,
    write_latest_pointer,
)
from tests.audio_validation_fixtures import NOW, _active_chip_inputs


def _artifact(**overrides) -> ValidationArtifact:
    values = {
        "validated_at": NOW,
        "mic_id": "xvf3800",
        "dac_id": "apple_usb_c_dongle",
        "profile": "xvf_chip_aec",
        "status": "pass",
        "checks": {
            "xvf_profile_readback": "pass",
            "drift_ppm_30m": 0.9,
            "outputd_chip_ref_health": {
                "status": "pass",
                "xruns": 0,
            },
        },
        "recommendation": "chip_aec_viable",
    }
    values.update(overrides)
    return ValidationArtifact(**values)


def test_write_and_load_artifact_round_trip(tmp_path):
    artifact = _artifact(notes=("direct fanout lab pass",))

    path = write_artifact(artifact, directory=tmp_path)
    result = load_artifact(path, now=NOW + timedelta(days=1))

    assert result.state == "loaded"
    assert result.artifact == artifact
    assert result.ok is True
    assert result.path == path
    assert result.stale is False
    assert path.name.endswith("__xvf3800__apple_usb_c_dongle__xvf_chip_aec__pass.json")
    assert path.read_text(encoding="utf-8").endswith("\n")


def test_latest_pointer_is_convenience_not_timestamped_record(tmp_path):
    artifact = _artifact(notes=("direct fanout lab pass",))

    durable_path = write_artifact(artifact, directory=tmp_path)
    latest_path = write_latest_pointer(artifact, directory=tmp_path)
    latest = load_artifact(latest_path, now=NOW + timedelta(days=1))

    assert durable_path.name != "latest.json"
    assert latest_path.name == "latest.json"
    assert latest.artifact == artifact


@pytest.mark.parametrize("writer", [write_artifact, write_latest_pointer])
@pytest.mark.parametrize(
    ("expected_mode", "writer_kwargs"),
    [
        (0o644, {}),
        (0o640, {"file_mode": 0o640}),
    ],
)
def test_artifact_writers_preserve_exact_content_and_mode(
    tmp_path,
    writer,
    expected_mode,
    writer_kwargs,
):
    artifact = _artifact(notes=("direct fanout lab pass",))
    expected_body = (
        json.dumps(artifact.to_dict(), allow_nan=False, indent=2, sort_keys=True) + "\n"
    )

    path = writer(artifact, directory=tmp_path, **writer_kwargs)

    assert path.read_text(encoding="utf-8") == expected_body
    assert stat.S_IMODE(path.stat().st_mode) == expected_mode


def test_write_artifact_sanitizes_filename_components(tmp_path):
    artifact = _artifact(
        mic_id="Seeed XVF3800 / test unit",
        dac_id="../Apple dongle",
        profile="xvf chip aec",
    )

    path = write_artifact(artifact, directory=tmp_path)

    assert path.parent == tmp_path
    assert "/" not in path.name
    assert ".." not in path.name


def test_write_artifact_cleans_temp_file_on_replace_error(tmp_path, monkeypatch):
    artifact = _artifact()

    def fail_replace(_src, _dst):
        raise OSError("replace failed")

    monkeypatch.setattr(artifacts.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        write_artifact(artifact, directory=tmp_path)

    assert list(tmp_path.glob("*.tmp")) == []


def test_make_artifact_defaults_timestamp_to_timezone_aware_utc():
    artifact = make_artifact(
        mic_id="xvf3800",
        dac_id="apple_usb_c_dongle",
        profile="xvf_chip_aec",
        status="unknown",
        checks={},
        recommendation="run_validation",
    )

    assert artifact.validated_at.tzinfo is not None


def test_load_missing_returns_missing_result(tmp_path):
    result = load_artifact(tmp_path / "missing.json")

    assert result == ArtifactLoadResult(
        state="missing",
        path=tmp_path / "missing.json",
        errors=("artifact file does not exist",),
    )


def test_load_malformed_json_returns_malformed(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")

    result = load_artifact(path)

    assert result.state == "malformed"
    assert result.artifact is None
    assert "invalid JSON" in result.errors[0]


@pytest.mark.parametrize(
    ("payload", "expected_issue"),
    [
        ({}, "schema_version"),
        (
            {
                "schema_version": 1,
                "validated_at": "2026-06-01T12:00:00",
                "hardware": {"mic_id": "xvf3800", "dac_id": "apple"},
                "profile": "xvf_chip_aec",
                "status": "pass",
                "checks": {},
                "recommendation": "ok",
            },
            "validated_at must include a timezone",
        ),
        (
            {
                "schema_version": 1,
                "validated_at": "2026-06-01T12:00:00Z",
                "hardware": {"mic_id": "xvf3800", "dac_id": "apple"},
                "profile": "xvf_chip_aec",
                "status": "maybe",
                "checks": {},
                "recommendation": "ok",
            },
            "status must be one of",
        ),
        (
            {
                "schema_version": 1,
                "validated_at": "2026-06-01T12:00:00Z",
                "hardware": {"mic_id": "xvf3800", "dac_id": "apple"},
                "profile": "xvf_chip_aec",
                "status": "pass",
                "checks": [],
                "recommendation": "ok",
            },
            "checks must be an object",
        ),
    ],
)
def test_parse_artifact_payload_reports_schema_issues(payload, expected_issue):
    with pytest.raises(ValidationArtifactError) as excinfo:
        parse_artifact_payload(payload)

    assert expected_issue in str(excinfo.value)


def test_parse_artifact_payload_accepts_optional_note_and_error_strings():
    artifact = parse_artifact_payload(
        {
            "schema_version": 1,
            "validated_at": "2026-06-01T12:00:00Z",
            "hardware": {"mic_id": "xvf3800", "dac_id": "apple_usb_c_dongle"},
            "profile": "xvf_chip_aec",
            "status": "fail",
            "checks": {"drift_ppm_30m": 46.0},
            "recommendation": "use_software_aec3",
            "notes": "old feeder path",
            "errors": ["drift exceeded gate"],
        }
    )

    assert artifact.notes == ("old feeder path",)
    assert artifact.errors == ("drift exceeded gate",)


def test_load_artifact_marks_stale_when_older_than_threshold(tmp_path):
    artifact = _artifact(validated_at=NOW - timedelta(days=31))
    path = write_artifact(artifact, directory=tmp_path)

    result = load_artifact(path, now=NOW)

    assert result.state == "stale"
    assert result.artifact == artifact
    assert result.ok is False
    assert result.stale is True


def test_load_artifact_rejects_future_timestamps_beyond_skew(tmp_path):
    artifact = _artifact(validated_at=NOW + timedelta(minutes=10))
    path = write_artifact(artifact, directory=tmp_path)

    result = load_artifact(path, now=NOW)

    assert result.state == "future"
    assert result.artifact == artifact
    assert result.ok is False
    assert "future" in result.errors[0]


def test_freshness_helpers_support_custom_thresholds():
    artifact = _artifact(validated_at=NOW - timedelta(hours=3))

    assert artifact_age(artifact, now=NOW) == timedelta(hours=3)
    assert is_artifact_stale(
        artifact,
        now=NOW,
        max_age=timedelta(hours=2),
    )
    assert not is_artifact_stale(
        artifact,
        now=NOW,
        max_age=timedelta(hours=4),
    )
    assert not is_artifact_stale(artifact, now=NOW, max_age=None)
    assert is_artifact_from_future(
        _artifact(validated_at=NOW + timedelta(minutes=10)),
        now=NOW,
    )
    assert not is_artifact_from_future(
        _artifact(validated_at=NOW + timedelta(minutes=1)),
        now=NOW,
    )


def test_load_latest_artifact_picks_newest_matching_valid_payload(tmp_path):
    old = _artifact(validated_at=NOW - timedelta(days=2), status="fail")
    newest_other = _artifact(
        validated_at=NOW,
        dac_id="hifiberry_dac8x",
        recommendation="use_software_aec3",
    )
    newest_match = _artifact(
        validated_at=NOW - timedelta(hours=1),
        status="pass",
    )
    write_artifact(old, directory=tmp_path)
    write_artifact(newest_other, directory=tmp_path)
    expected_path = write_artifact(newest_match, directory=tmp_path)

    result = load_latest_artifact(
        tmp_path,
        mic_id="xvf3800",
        dac_id="apple_usb_c_dongle",
        profile="xvf_chip_aec",
        now=NOW,
    )

    assert result.state == "loaded"
    assert result.artifact == newest_match
    assert result.path == expected_path


def test_load_latest_artifact_reports_malformed_when_no_valid_artifacts(tmp_path):
    (tmp_path / "bad.json").write_text("[]", encoding="utf-8")

    result = load_latest_artifact(tmp_path)

    assert result.state == "malformed"
    assert result.artifact is None
    assert "artifact must be a JSON object" in result.errors[0]


def test_load_latest_artifact_keeps_malformed_errors_when_valid_artifact_exists(
    tmp_path,
):
    (tmp_path / "bad.json").write_text("{bad", encoding="utf-8")
    artifact = _artifact()
    write_artifact(artifact, directory=tmp_path)

    result = load_latest_artifact(tmp_path, now=NOW)

    assert result.state == "loaded"
    assert result.artifact == artifact
    assert result.errors


def test_load_latest_artifact_surfaces_future_artifact_even_with_older_pass(
    tmp_path,
):
    old = _artifact(validated_at=NOW - timedelta(days=1))
    future = _artifact(validated_at=NOW + timedelta(minutes=10))
    write_artifact(old, directory=tmp_path)
    expected_path = write_artifact(future, directory=tmp_path)

    result = load_latest_artifact(tmp_path, now=NOW)

    assert result.state == "future"
    assert result.artifact == future
    assert result.path == expected_path
    assert result.ok is False


def test_validation_artifact_rejects_non_json_check_values():
    with pytest.raises(ValidationArtifactError) as excinfo:
        _artifact(checks={"not_json": object()})

    assert "not_json" in str(excinfo.value)


def test_validation_artifact_rejects_non_finite_check_values():
    with pytest.raises(ValidationArtifactError) as excinfo:
        _artifact(checks={"drift_ppm_30m": math.nan})

    assert "drift_ppm_30m" in str(excinfo.value)


def test_to_dict_uses_manually_inspectable_schema():
    artifact = _artifact(errors=("drift unstable",))

    data = artifact.to_dict()

    assert data["schema_version"] == 1
    assert data["validated_at"] == "2026-06-01T16:00:00Z"
    assert data["hardware"] == {
        "mic_id": "xvf3800",
        "dac_id": "apple_usb_c_dongle",
    }
    assert data["profile"] == "xvf_chip_aec"
    assert data["status"] == "pass"
    assert data["recommendation"] == "chip_aec_viable"
    assert data["errors"] == ["drift unstable"]
    json.dumps(data)


def test_latest_artifact_summary_reads_timestamped_artifacts(tmp_path):
    artifact = audio_validation.build_chip_aec_readiness_artifact(
        **_active_chip_inputs(),
    )
    path = artifacts.write_artifact(artifact, directory=tmp_path)
    summary = artifacts.latest_artifact_summary(
        path=tmp_path,
        requested_profile="xvf_chip_aec",
        now=NOW,
    )

    assert path.name != "latest.json"
    assert summary["state"] == "current"
    assert summary["status"] == "pass"
    assert summary["artifact_path"] == str(path)
    assert summary["hardware"] == {
        "mic_id": "xvf3800",
        "dac_id": "apple_usb_c_dongle",
    }


def test_latest_artifact_summary_prefers_latest_pointer(tmp_path):
    artifact = audio_validation.build_chip_aec_readiness_artifact(
        **_active_chip_inputs(),
    )
    artifacts.write_artifact(artifact, directory=tmp_path)
    latest_path = artifacts.write_latest_pointer(artifact, directory=tmp_path)

    summary = artifacts.latest_artifact_summary(
        path=tmp_path,
        requested_profile="xvf_chip_aec",
        now=NOW,
    )

    assert summary["artifact_path"] == str(latest_path)
    assert summary["status"] == "pass"
    assert "reason" not in summary


def test_latest_artifact_summary_falls_back_from_stale_mismatched_latest(
    tmp_path,
):
    stale_other = make_artifact(
        validated_at=NOW - timedelta(days=31),
        mic_id="xvf3800",
        dac_id="apple_usb_c_dongle",
        profile="xvf_software_aec3",
        status="pass",
        checks={"measured_drift_delay": {"status": "pass"}},
        recommendation="use_software_aec3",
    )
    matching = make_artifact(
        validated_at=NOW,
        mic_id="xvf3800",
        dac_id="apple_usb_c_dongle",
        profile="xvf_chip_aec",
        status="warn",
        checks={"measured_drift_delay": {"status": "not_run"}},
        recommendation="run_hardware_validation",
    )
    matching_path = artifacts.write_artifact(matching, directory=tmp_path)
    artifacts.write_latest_pointer(stale_other, directory=tmp_path)

    summary = artifacts.latest_artifact_summary(
        path=tmp_path,
        requested_profile="xvf_chip_aec",
        now=NOW,
    )

    assert summary["state"] == "current"
    assert summary["artifact_path"] == str(matching_path)
    assert summary["profile"] == "xvf_chip_aec"
    assert summary["reason"].startswith("latest.json ignored:")


def test_latest_artifact_summary_falls_back_from_malformed_latest(tmp_path):
    matching = make_artifact(
        validated_at=NOW,
        mic_id="xvf3800",
        dac_id="apple_usb_c_dongle",
        profile="xvf_chip_aec",
        status="warn",
        checks={"measured_drift_delay": {"status": "not_run"}},
        recommendation="run_hardware_validation",
    )
    matching_path = artifacts.write_artifact(matching, directory=tmp_path)
    (tmp_path / "latest.json").write_text("{bad", encoding="utf-8")

    summary = artifacts.latest_artifact_summary(
        path=tmp_path,
        requested_profile="xvf_chip_aec",
        now=NOW,
    )

    assert summary["state"] == "current"
    assert summary["artifact_path"] == str(matching_path)
    assert summary["profile"] == "xvf_chip_aec"
    assert "latest.json ignored: invalid JSON" in summary["reason"]


def test_latest_artifact_summary_marks_stale_and_profile_mismatch(tmp_path):
    artifact = make_artifact(
        validated_at=NOW - timedelta(days=31),
        mic_id="xvf3800",
        dac_id="apple_usb_c_dongle",
        profile="xvf_chip_aec",
        status="warn",
        checks={"measured_drift_delay": {"status": "not_run"}},
        recommendation="run_hardware_validation",
    )
    artifact_path = artifacts.write_artifact(artifact, directory=tmp_path)

    stale = artifacts.latest_artifact_summary(
        path=tmp_path,
        requested_profile="xvf_chip_aec",
        now=NOW,
    )
    mismatch = artifacts.latest_artifact_summary(
        path=artifact_path,
        requested_profile="xvf_software_aec3",
        now=NOW,
    )

    assert stale["state"] == "stale"
    assert mismatch["state"] == "mismatch"
    assert mismatch["available"] is True
