# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from pathlib import Path

from jasper.correction import acoustic_quality, bundles, evidence

from .correction_bundle_fixtures import write_golden_correction_bundle


def _write_repeat_bundle(
    root: Path,
    session_id: str,
    magnitudes: list[float],
    *,
    mic_serial: str = "abc",
    input_device: dict | None = None,
) -> Path:
    bundle = root / session_id
    bundle.mkdir(parents=True)
    info = {
        "bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
        "session_id": session_id,
        "state": "ready",
        "current_position": 1,
        "total_positions": 1,
        "mic_calibration": {"model_key": "umik_1", "serial": mic_serial},
        "input_device": input_device or {"deviceId": "usb-mic", "label": "USB mic"},
        "capture_quality": [],
        "runtime_integrity": {"level": "ok", "issues": []},
    }
    result = {
        "bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
        "session_id": session_id,
        "measured": {
            "freqs_hz": [50, 80, 160, 320, 500],
            "magnitude_db": magnitudes,
        },
        "target": {
            "freqs_hz": [50, 80, 160, 320, 500],
            "magnitude_db": [0, 0, 0, 0, 0],
        },
        "confidence_report": {"level": "medium", "score": 70},
    }
    (bundle / "info.json").write_text(json.dumps(info))
    (bundle / "result.json").write_text(json.dumps(result))
    return bundle


def test_acoustic_quality_report_promotes_snr_to_summary():
    report = acoustic_quality.build_acoustic_quality_report(
        session_id="abc",
        capture_quality=[{
            "capture_kind": "measurement",
            "position_index": 0,
            "artifact_path": "captures/p0.wav",
            "rms_dbfs": -32.0,
            "peak_dbfs": -12.0,
            "noise_floor_dbfs": -70.0,
            "estimated_snr_db": 38.0,
            "issues": [],
        }],
    )

    assert report["summary"]["level"] == "ok"
    assert report["summary"]["snr_level"] == "high"
    assert report["summary"]["min_estimated_snr_db"] == 38.0


def test_evidence_repeatability_scores_matching_bundles(tmp_path: Path):
    first = _write_repeat_bundle(tmp_path, "first", [0, 1, 2, 1, 0])
    second = _write_repeat_bundle(tmp_path, "second", [0, 1.2, 2.1, 0.9, 0])

    repeatability = evidence.compare_bundle_repeatability(first, second)

    assert repeatability["available"] is True
    assert repeatability["level"] == "high"
    assert repeatability["metrics"]["rms_db"] < 0.2


def test_evidence_repeatability_compares_sanitized_input_hashes(
    tmp_path: Path,
):
    first = _write_repeat_bundle(
        tmp_path,
        "first",
        [0, 1, 2, 1, 0],
        input_device={
            "device_id_hash": "hash-a",
            "actual_device_id_hash": "hash-a",
            "label": "USB mic",
        },
    )
    second = _write_repeat_bundle(
        tmp_path,
        "second",
        [0, 1.1, 2.0, 0.9, 0],
        input_device={
            "device_id_hash": "hash-b",
            "actual_device_id_hash": "hash-b",
            "label": "USB mic",
        },
    )

    repeatability = evidence.compare_bundle_repeatability(first, second)

    assert repeatability["level"] == "high"
    codes = {issue["code"] for issue in repeatability["issues"]}
    assert "repeatability_input_device_mismatch" in codes


def test_evidence_repeatability_ignores_browser_label_drift(
    tmp_path: Path,
):
    first = _write_repeat_bundle(
        tmp_path,
        "first",
        [0, 1, 2, 1, 0],
        input_device={
            "actual_device_id_hash": "same-device",
            "label": "USB Measurement Mic",
        },
    )
    second = _write_repeat_bundle(
        tmp_path,
        "second",
        [0, 1.1, 2.0, 0.9, 0],
        input_device={
            "actual_device_id_hash": "same-device",
            "label": "Different Browser Label",
        },
    )

    repeatability = evidence.compare_bundle_repeatability(first, second)

    codes = {issue["code"] for issue in repeatability["issues"]}
    assert "repeatability_input_device_mismatch" not in codes


def test_evidence_packet_keeps_low_repeatability_as_caution(tmp_path: Path):
    first = _write_repeat_bundle(tmp_path, "first", [0, 1, 2, 1, 0])
    result_path = first / "result.json"
    result = json.loads(result_path.read_text())
    result["confidence_report"]["strategy_gates"] = {
        "safe": {"allowed": True, "reasons": []},
        "balanced": {"allowed": True, "reasons": []},
        "assertive": {"allowed": True, "reasons": []},
        "future_fir": {"allowed": True, "reasons": []},
    }
    result_path.write_text(json.dumps(result))
    second = _write_repeat_bundle(
        tmp_path,
        "second",
        [0, 7, -6, 5, 0],
        mic_serial="different",
    )

    packet = evidence.build_evidence_packet(
        first,
        repeat_bundle_dir=second,
    )

    assert packet["agent_readiness"]["level"] == "caution"
    assert packet["artifact_schema_version"] == 2
    assert packet["repeatability"]["level"] == "low"
    assert (
        packet["capability_permissions"]["permissions"]["safe_peq"]["allowed"]
        is True
    )
    assert (
        packet["capability_permissions"]["permissions"]["balanced_peq"]["allowed"]
        is True
    )
    assert (
        packet["capability_permissions"]["permissions"]["assertive_peq"]["allowed"]
        is False
    )
    assert (
        packet["capability_permissions"]["permissions"]["future_fir"]["allowed"]
        is False
    )
    assert any(
        item["code"] == "position_analysis_missing"
        for item in packet["missing_evidence"]
    )
    codes = {issue["code"] for issue in packet["repeatability"]["issues"]}
    assert "repeatability_low" in codes
    assert "repeatability_mic_mismatch" in codes
    assert packet["side_effects"] == []


def test_golden_bundle_fixture_builds_ready_evidence_packet(tmp_path: Path):
    bundle = write_golden_correction_bundle(tmp_path)

    packet = evidence.build_evidence_packet(bundle)

    assert packet["artifact_schema_version"] == evidence.SCHEMA_VERSION
    assert packet["bundle"]["has_artifact_manifest"] is True
    assert packet["bundle"]["issues"] == []
    assert packet["agent_readiness"]["level"] == "ready"
    assert packet["capability_permissions"]["permissions"]["safe_peq"]["allowed"]
    assert packet["repeatability"]["level"] == "high"
    assert packet["missing_evidence"] == []
